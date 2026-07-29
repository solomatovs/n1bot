import time
from typing import Any, ClassVar, Optional, cast
from uuid import UUID

from chainlit.context import context_var
from chainlit.langchain.callbacks import (
    FinalStreamHelper,
    GenerationHelper,
)
from chainlit.message import Message
from chainlit.step import Step
from chainlit.utils import utc_now
from langchain_core.messages import BaseMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk, GenerationChunk
from langchain_core.tracers.base import AsyncBaseTracer
from langchain_core.tracers.schemas import Run
from literalai import ChatGeneration, CompletionGeneration
from literalai.observability.step import TrueStepType

from boba.chainlit2.agent.model import ReasoningChatOpenAI


class BobaLangchainTracer(AsyncBaseTracer, GenerationHelper, FinalStreamHelper):
    steps: dict[str, Step]
    reasoning_steps: dict[str, Step]
    parent_id_map: dict[str, str]
    ignored_runs: set

    # типы run, которые показываем в UI. всё остальное — служебная обвязка
    VISIBLE_RUN_TYPES: ClassVar[frozenset[str]] = frozenset(
        {"llm", "tool", "retriever"}
    )

    # заголовок шага, в который стримятся рассуждения модели
    REASONING_STEP_NAME: ClassVar[str] = "Думаю"

    # запасной заголовок llm-шага, если провайдер не сообщил имя модели
    LLM_STEP_NAME: ClassVar[str] = "Ответ"

    # ключ в metadata тула, которым он объявляет подачу своего результата:
    # "markdown" — отрендерить разметку, любое другое значение — блок кода
    # с этой подсветкой ("json", "sql", "python", "text")
    UI_FORMAT_KEY: ClassVar[str] = "ui_format"
    MARKDOWN_FORMAT: ClassVar[str] = "markdown"

    def __init__(
        self,
        # Token sequence that prefixes the answer
        answer_prefix_tokens: list[str] | None = None,
        # Should we stream the final answer?
        stream_final_answer: bool = False,
        # Should force stream the first response?
        force_stream_final_answer: bool = False,
        **kwargs: Any,
    ) -> None:
        AsyncBaseTracer.__init__(self, **kwargs)
        GenerationHelper.__init__(self)
        FinalStreamHelper.__init__(
            self,
            answer_prefix_tokens=answer_prefix_tokens,
            stream_final_answer=stream_final_answer,
            force_stream_final_answer=force_stream_final_answer,
        )
        self.context = context_var.get()
        self.steps = {}
        self.reasoning_steps = {}
        self.parent_id_map = {}
        self.ignored_runs = set()

        if self.context.current_step:
            self.root_parent_id = self.context.current_step.id
        else:
            self.root_parent_id = None

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: "UUID",
        parent_run_id: Optional["UUID"] = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        name: str | None = None,
        **kwargs: Any,
    ) -> Run:
        lc_messages = messages[0]
        self.chat_generations[str(run_id)] = {
            "input_messages": lc_messages,
            "start": time.time(),
            "token_count": 0,
            "tt_first_token": None,
        }

        return await super().on_chat_model_start(
            serialized,
            messages,
            run_id=run_id,
            parent_run_id=parent_run_id,
            tags=tags,
            metadata=metadata,
            name=name,
            **kwargs,
        )

    async def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: "UUID",
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        await super().on_llm_start(
            serialized,
            prompts,
            run_id=run_id,
            parent_run_id=parent_run_id,
            tags=tags,
            metadata=metadata,
            **kwargs,
        )

        self.completion_generations[str(run_id)] = {
            "prompt": prompts[0],
            "start": time.time(),
            "token_count": 0,
            "tt_first_token": None,
        }

    async def on_llm_new_token(
        self,
        token: str,
        *,
        chunk: GenerationChunk | ChatGenerationChunk | None = None,
        run_id: "UUID",
        parent_run_id: Optional["UUID"] = None,
        **kwargs: Any,
    ) -> None:
        await super().on_llm_new_token(
            token=token,
            chunk=chunk,
            run_id=run_id,
            parent_run_id=parent_run_id,
            **kwargs,
        )
        if isinstance(chunk, ChatGenerationChunk):
            start = self.chat_generations[str(run_id)]
        else:
            start = self.completion_generations[str(run_id)]  # type: ignore
        start["token_count"] += 1
        if start["tt_first_token"] is None:
            start["tt_first_token"] = (time.time() - start["start"]) * 1000

        await self._stream_reasoning(str(run_id), chunk)

        # Process token to ensure it's a string, as strip() will be called on it.
        processed_token: str
        # Handle case where token is a list (can occur with some model outputs).
        # Join all elements into a single string to maintain
        # compatibility with downstream processing.
        if isinstance(token, list):
            # If token is a list, join its elements (converted to strings)
            # into a single string.
            processed_token = "".join(map(str, token))
        elif not isinstance(token, str):
            # If token is neither a list nor a string, convert it to a string.
            processed_token = str(token)
        else:
            # If token is already a string, use it as is.
            processed_token = token

        if self.stream_final_answer:
            self._append_to_last_tokens(processed_token)

            if self.answer_reached:
                if not self.final_stream:
                    self.final_stream = Message(content="")
                    await self.final_stream.send()
                await self.final_stream.stream_token(processed_token)
                self.has_streamed_final_answer = True
            else:
                self.answer_reached = self._check_if_answer_reached()

    async def _stream_reasoning(
        self,
        run_id: str,
        chunk: GenerationChunk | ChatGenerationChunk | None,
    ) -> None:
        """Льёт reasoning_content провайдера в отдельный шаг рядом с llm-шагом."""
        if not isinstance(chunk, ChatGenerationChunk):
            return

        reasoning = chunk.message.additional_kwargs.get(
            ReasoningChatOpenAI.REASONING_KEY
        )
        if not reasoning:
            return

        context_var.set(self.context)

        step = self.reasoning_steps.get(run_id)
        if step is None:
            llm_step = self.steps.get(run_id)
            step = Step(
                name=self.REASONING_STEP_NAME,
                type="llm",
                parent_id=llm_step.parent_id if llm_step else self.root_parent_id,
            )
            step.start = utc_now()
            await step.send()
            self.reasoning_steps[run_id] = step

        await step.stream_token(str(reasoning))

    async def _close_reasoning(self, run_id: str) -> None:
        """Закрывает шаг рассуждений вместе с породившим его llm-run'ом."""
        if step := self.reasoning_steps.pop(run_id, None):
            step.end = utc_now()
            await step.update()

    async def _persist_run(self, run: Run) -> None:
        pass

    def _get_run_parent_id(self, run: Run):
        parent_id = str(run.parent_run_id) if run.parent_run_id else self.root_parent_id

        return parent_id

    def _get_non_ignored_parent_id(self, current_parent_id: str | None = None):
        if not current_parent_id:
            return self.root_parent_id

        if current_parent_id not in self.parent_id_map:
            return None

        while current_parent_id in self.parent_id_map:
            # If the parent id is in the ignored runs, we need to get the parent id of the ignored run
            if current_parent_id in self.ignored_runs:
                current_parent_id = self.parent_id_map[current_parent_id]
            else:
                return current_parent_id

        return self.root_parent_id

    def _should_ignore_run(self, run: Run) -> tuple[bool, str | None]:
        """Оставляет в UI только run'ы из VISIBLE_RUN_TYPES."""
        parent_id = self._get_run_parent_id(run)

        if parent_id:
            # Add the parent id of the ignored run in the mapping
            # so we can re-attach a kept child to the right parent id
            self.parent_id_map[str(run.id)] = parent_id

        if run.run_type in self.VISIBLE_RUN_TYPES:
            # родительские ноды скрыты, поэтому шаг переприцепляем
            # к ближайшему видимому предку
            return False, self._get_non_ignored_parent_id(parent_id)

        self.ignored_runs.add(str(run.id))
        return True, parent_id

    async def _start_trace(self, run: Run) -> None:
        await super()._start_trace(run)
        context_var.set(self.context)

        ignore, parent_id = self._should_ignore_run(run)

        if run.run_type in ["chain", "prompt"]:
            self.generation_inputs[str(run.id)] = cast(
                "dict[Any, Any]", self.ensure_values_serializable(run.inputs)
            )

        if ignore:
            return

        step_type: TrueStepType = "llm" if run.run_type == "llm" else "tool"

        step = Step(
            id=str(run.id),
            name=self._step_name(run),
            type=step_type,
            parent_id=parent_id,
        )
        step.start = utc_now()
        if step_type == "tool":
            step.input = run.inputs
            step.show_input = "json"
        else:
            # у llm-шага содержимое даёт generation, отдельный input дублировал бы
            # его сериализованным state'ом
            step.show_input = False

        step.tags = run.tags
        self.steps[str(run.id)] = step

        await step.send()

    async def _on_run_update(self, run: Run) -> None:
        """Process a run upon update."""
        context_var.set(self.context)

        ignore, _parent_id = self._should_ignore_run(run)

        await self._close_reasoning(str(run.id))

        if ignore:
            return

        current_step = self.steps.get(str(run.id), None)

        if run.run_type == "llm" and current_step:
            # run_type == "llm" ⇒ serialized непустой, ветка tuple[None, None]
            # недостижима; фиксируем 4-кортеж для распаковки
            provider, model, tools, llm_settings = cast(
                "tuple[Any, Any, Any, Any]",
                self._build_llm_settings(
                    (run.serialized or {}), (run.extra or {}).get("invocation_params")
                ),
            )
            generations = (run.outputs or {}).get("generations", [])
            generation = generations[0][0]
            variables = self.generation_inputs.get(str(run.parent_run_id), {})
            variables = {k: str(v) for k, v in variables.items() if v is not None}
            if message := generation.get("message"):
                chat_start = self.chat_generations[str(run.id)]
                duration = time.time() - chat_start["start"]
                if duration and chat_start["token_count"]:
                    throughput = chat_start["token_count"] / duration
                else:
                    throughput = None
                message_completion = self._convert_message(message)
                current_step.generation = ChatGeneration(
                    provider=provider,
                    model=model,
                    tools=tools,
                    variables=variables,
                    settings=llm_settings,
                    duration=duration,
                    token_throughput_in_s=throughput,
                    tt_first_token=chat_start.get("tt_first_token"),
                    messages=[
                        self._convert_message(m) for m in chat_start["input_messages"]
                    ],
                    message_completion=message_completion,
                )

                # find first message with prompt_id
                for m in chat_start["input_messages"]:
                    if m.additional_kwargs.get("prompt_id"):
                        current_step.generation.prompt_id = m.additional_kwargs[
                            "prompt_id"
                        ]
                        if custom_variables := m.additional_kwargs.get("variables"):
                            current_step.generation.variables = {
                                k: str(v)
                                for k, v in custom_variables.items()
                                if v is not None
                            }
                    break

                current_step.language = "json"
            else:
                completion_start = self.completion_generations[str(run.id)]
                completion = generation.get("text", "")
                duration = time.time() - completion_start["start"]
                if duration and completion_start["token_count"]:
                    throughput = completion_start["token_count"] / duration
                else:
                    throughput = None
                current_step.generation = CompletionGeneration(
                    provider=provider,
                    model=model,
                    settings=llm_settings,
                    variables=variables,
                    duration=duration,
                    token_throughput_in_s=throughput,
                    tt_first_token=completion_start.get("tt_first_token"),
                    prompt=completion_start["prompt"],
                    completion=completion,
                )
                current_step.output = completion

            if current_step:
                current_step.end = utc_now()
                await current_step.update()

            if self.final_stream and self.has_streamed_final_answer:
                await self.final_stream.update()

            return

        if current_step:
            if current_step.type != "llm":
                # Step сам выбирает подачу: строку отдаёт как markdown,
                # структуру сериализует в json и проставляет language
                current_step.output = self._unwrap_output(run.outputs)
                current_step.language = self._output_language(run, current_step)
            current_step.end = utc_now()
            await current_step.update()

    @classmethod
    def _output_language(cls, run: Run, step: Step) -> str | None:
        """Подсветка результата: объявленная в туле, иначе выбранная Step."""
        metadata = (run.extra or {}).get("metadata") or {}
        declared = metadata.get(cls.UI_FORMAT_KEY)
        if not declared:
            return step.language

        # непустой language оборачивает вывод в блок кода, а markdown
        # нужно именно отрендерить
        if declared == cls.MARKDOWN_FORMAT:
            return None

        return str(declared)

    @classmethod
    def _step_name(cls, run: Run) -> str:
        """Заголовок шага: для llm — имя модели, для тула — имя тула."""
        if run.run_type != "llm":
            return run.name

        # run.name у llm-шага это имя python-класса обёртки (ChatOpenAI и
        # наследники), пользователю оно ничего не говорит
        invocation_params = (run.extra or {}).get("invocation_params") or {}
        model = invocation_params.get("model") or invocation_params.get("model_name")

        return str(model) if model else cls.LLM_STEP_NAME

    @staticmethod
    def _unwrap_output(outputs: dict[str, Any] | None) -> Any:
        """Достаёт полезную нагрузку из outputs tool-run'а."""
        if not outputs:
            return None

        # langchain кладёт результат тула под ключ output, а langgraph
        # оборачивает его в ToolMessage
        value = outputs.get("output", outputs) if "output" in outputs else outputs
        if isinstance(value, ToolMessage):
            return value.content

        return value

    async def _on_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any):
        context_var.set(self.context)

        await self._close_reasoning(str(run_id))

        if current_step := self.steps.get(str(run_id), None):
            current_step.is_error = True
            current_step.output = str(error)
            current_step.end = utc_now()
            await current_step.update()

    on_llm_error = _on_error
    on_chain_error = _on_error
    on_tool_error = _on_error
    on_retriever_error = _on_error
