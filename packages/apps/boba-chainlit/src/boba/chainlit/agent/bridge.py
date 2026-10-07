"""Мост ChatModel -> langchain BaseChatModel и перевод сообщений langchain.

Граф хода работает с BaseChatModel; мост конвертирует langchain-сообщения в
конверт ChatRequest, события модели — в чанки и итоговое сообщение. Какой
бэкенд за портом — мосту безразлично. LangchainMessages — единственное
место, где сообщения langchain становятся сообщениями диалога чата
(boba.chainlit.chat.dialog) и типами ядра и обратно: всё, что лежит в
additional_kwargs и response_metadata сообщений checkpoint'а, пишет и
читает он.

Ошибки:
pydantic.ValidationError — сообщение истории несёт результат известного
    вида, вызов или адрес журнала, поля которых не проходят модель.
LlmError — отказ бэкенда модели, уходит наверх как есть.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar, Final, Literal

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages import (
    ToolCall as LangchainToolCall,
)
from langchain_core.messages.ai import OutputTokenDetails, UsageMetadata
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import (
    ChatGeneration,
    ChatGenerationChunk,
    ChatResult,
    GenerationChunk,
    LLMResult,
)
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, Field, ValidationError
from typing_extensions import override

from boba.chainlit.chat.dialog import (
    AssistantMessage,
    Attachment,
    DialogMessage,
    LlmUsage,
    ToolReply,
    TurnMark,
    TurnRecord,
    UserMessage,
)
from boba.llm.chat import (
    ChatDelta,
    ChatModel,
    ChatReply,
    ChatRequest,
    ChatRole,
    ChatTurn,
    ToolCall,
    ToolSpec,
)
from boba.toolkit.dag import DagNode, JournalAddress, NodeOutcome
from boba.toolkit.failure import InvokeErrorKind
from boba.toolkit.result import ErrorResult, ToolArtifact

__all__ = ["ChatModelBridge", "LangchainMessages", "ResponseField"]


class ResponseField(StrEnum):
    """Ключи additional_kwargs сообщений langchain, которые пишет и читает чат."""

    REASONING_CONTENT = "reasoning_content"
    ATTACHMENTS = "attachments"
    PREFETCH_ELAPSED = "prefetch_elapsed_ms"


class AttachmentField(StrEnum):
    """Ключи записи вложения в additional_kwargs вопроса пользователя."""

    NAME = "name"
    PATH = "path"


class UsageField:
    """Ключи langchain-UsageMetadata: у TypedDict pyright принимает только литерал."""

    INPUT: Final = "input_tokens"
    OUTPUT: Final = "output_tokens"
    OUTPUT_DETAILS: Final = "output_token_details"
    REASONING: Final = "reasoning"


class LangchainMessages:
    """Перевод между сообщениями langchain и типами чата и ядра.

    Создаётся каждым, кто стоит в каталоге agent на стыке с langchain:
    чтением checkpoint'ов и агентом сессии, переводчиком событий модели,
    стыком графа с портом инструментов (LangchainPort) и подготовкой хода.
    Сообщение checkpoint'а переводит в сообщение диалога чата (dialog) и
    обратно собирает то, что чат пишет в историю: вопрос пользователя
    (human), запись оборванного хода (record), вызовы подготовки
    (prefetch_request), итог вызова инструмента (tool_message). Другого
    места, где читаются и пишутся additional_kwargs и response_metadata
    сообщений истории, нет.
    """

    JOURNAL_KEY: ClassVar[str] = "boba_journal"
    """Ключ response_metadata сообщения инструмента с адресом журнала вызова."""

    def dialog(self, messages: Sequence[object]) -> Iterator[DialogMessage]:
        """Сообщения диалога из сообщений checkpoint'а; системные и
        служебные записи графа к диалогу не относятся.

        Ошибки:
        pydantic.ValidationError — сообщение несёт результат известного
            вида, вызов или адрес журнала, поля которых не проходят модель.
        """
        for message in messages:
            match message:
                case HumanMessage():
                    yield self.user(message)
                case ToolMessage():
                    yield self.reply(message)
                case AIMessage():
                    yield self.assistant(message)
                case _:
                    continue

    def user(self, message: HumanMessage) -> UserMessage:
        """Вопрос пользователя из сообщения истории."""
        return UserMessage(
            id=message.id,
            text=self._shown_text(message),
            attachments=tuple(self._attachments(message)),
        )

    def assistant(self, message: AIMessage) -> AssistantMessage:
        """Ответ ассистента из сообщения истории или ответа модели.

        Ошибки:
        pydantic.ValidationError — аргументы вызова инструмента не JSON.
        """
        extra = message.additional_kwargs

        mark: TurnMark | None = None
        if extra.get(TurnMark.STOPPED.value):
            mark = TurnMark.STOPPED
        if extra.get(TurnMark.ERROR.value):
            mark = TurnMark.ERROR

        elapsed = extra.get(ResponseField.PREFETCH_ELAPSED.value)
        if not isinstance(elapsed, int):
            elapsed = 0

        return AssistantMessage(
            id=message.id,
            text=self._shown_text(message),
            reasoning=self.reasoning_of(message),
            calls=tuple(self._calls(message)),
            usage=self._usage(message),
            mark=mark,
            prefetch_elapsed_ms=elapsed,
        )

    def reply(self, message: ToolMessage) -> ToolReply:
        """Ответ инструмента из сообщения истории.

        Ошибки:
        pydantic.ValidationError — kind результата известен, а поля модели
            либо адрес журнала не проходят.
        """
        text = self._shown_text(message)

        raw = message.artifact
        if raw is None:
            raw = text

        name = message.name
        if not name:
            name = ""

        return ToolReply(
            id=message.id,
            call_id=message.tool_call_id,
            name=name,
            text=text,
            outcome=self.outcome_of(message),
            raw=raw,
            journal=self.journal_of(message),
        )

    def human(self, question: UserMessage) -> HumanMessage:
        """Вопрос пользователя сообщением для графа и истории; вложения
        едут в additional_kwargs: в тексте вопроса их нет."""
        extra: dict[str, Any] = {}
        if question.attachments:
            listed: list[dict[str, str]] = []
            for attachment in question.attachments:
                listed.append(
                    {
                        AttachmentField.NAME.value: attachment.name,
                        AttachmentField.PATH.value: attachment.path,
                    }
                )

            extra[ResponseField.ATTACHMENTS.value] = listed

        return HumanMessage(
            content=question.text, id=question.id, additional_kwargs=extra
        )

    def record(self, record: TurnRecord) -> AIMessage:
        """Запись оборванного хода сообщением ассистента для истории: текст,
        пометка исхода и рассуждения, если они были."""
        extra: dict[str, Any] = {record.mark.value: True}
        if record.reasoning:
            extra[ResponseField.REASONING_CONTENT.value] = record.reasoning

        return AIMessage(content=record.content, additional_kwargs=extra)

    def prefetch_request(self, calls: Sequence[DagNode], elapsed_ms: int) -> AIMessage:
        """Вызовы подготовки хода сообщением ассистента для истории.

        Пустое поле рассуждений обязательно: провайдер в режиме размышления
        отклоняет сообщение с вызовами, у которого его нет, а подготовка
        ничего не обдумывала. Длительность подготовки едет пометкой: этап
        ленты собственного сообщения не имеет, и сборка истории узнаёт время
        только отсюда.
        """
        marks: dict[str, Any] = {
            ResponseField.REASONING_CONTENT.value: "",
            ResponseField.PREFETCH_ELAPSED.value: elapsed_ms,
        }

        tool_calls: list[LangchainToolCall] = []
        for call in calls:
            tool_calls.append(self.tool_call(call))

        return AIMessage(content="", tool_calls=tool_calls, additional_kwargs=marks)

    def of_chunk(
        self, chunk: GenerationChunk | ChatGenerationChunk | None
    ) -> BaseMessage | None:
        """Сообщение чанка; у текстовых чанков его нет."""
        if not isinstance(chunk, ChatGenerationChunk):
            return None

        return chunk.message

    def of_result(self, response: LLMResult) -> AssistantMessage | None:
        """Итоговое сообщение ответа модели; None — ответ без сообщения
        ассистента."""
        if not response.generations:
            return None

        first = response.generations[0]
        if not first:
            return None

        generation = first[0]
        if not isinstance(generation, ChatGeneration):
            return None

        message = generation.message
        if not isinstance(message, AIMessage):
            return None

        return self.assistant(message)

    def reasoning_of(self, message: BaseMessage | None) -> str:
        if message is None:
            return ""

        value = message.additional_kwargs.get(ResponseField.REASONING_CONTENT.value)
        if not value:
            return ""

        return str(value)

    def tool_call(self, node: DagNode) -> LangchainToolCall:
        """Вызов-узел записью вызова в сообщении ассистента истории."""
        return LangchainToolCall(
            name=node.tool, args=dict(node.args), id=node.key, type="tool_call"
        )

    def tool_message(self, outcome: NodeOutcome) -> ToolMessage:
        """Итог вызова сообщением инструмента для модели и истории. Адрес
        журнала вызова едет в response_metadata: история хранит его вместе
        с итогом, и журнал читается после конца хода."""
        status: Literal["success", "error"] = "success"
        if outcome.errored:
            status = "error"

        message = ToolMessage(
            content=outcome.content,
            artifact=outcome.artifact,
            name=outcome.tool,
            tool_call_id=outcome.key,
            status=status,
        )
        if outcome.journal is not None:
            stamped = outcome.journal.model_dump(mode="json")
            message.response_metadata[self.JOURNAL_KEY] = stamped

        return message

    def outcome_of(self, message: ToolMessage) -> NodeOutcome | None:
        """Итог вызова из сообщения инструмента истории — обратный перевод
        tool_message. None — в сообщении нет результата семейства и статус
        не ошибка: запись старой истории, её показывают сырым текстом.

        Ошибки:
        pydantic.ValidationError — kind результата известен, а поля модели
            либо адрес журнала не проходят.
        """
        errored = message.status == "error"
        artifact = ToolArtifact.revive(message.artifact)
        if artifact is None and errored:
            artifact = ErrorResult(
                message=self.text_of(message), error_kind=InvokeErrorKind.TOOL_ERROR
            )

        if artifact is None:
            return None

        name = message.name
        if not name:
            name = ""

        return NodeOutcome(
            key=message.tool_call_id,
            tool=name,
            content=self.text_of(message),
            artifact=artifact,
            errored=errored,
            journal=self.journal_of(message),
        )

    def journal_of(self, message: ToolMessage) -> JournalAddress | None:
        """Адрес журнала вызова из сообщения инструмента истории; None —
        вызов исполнял не сервер с журналом либо запись сделана в формате,
        по которому журнал уже не прочитать. От результата в сообщении не
        зависит: адрес несёт и запись без результата семейства.
        """
        stamped = message.response_metadata.get(self.JOURNAL_KEY)
        if not stamped:
            return None

        try:
            return JournalAddress.model_validate(stamped)
        except ValidationError:
            # запись старого формата: адрес не называет вызов на сервере,
            # журнала у такого шага нет
            return None

    @staticmethod
    def text_of(message: BaseMessage) -> str:
        """Содержимое сообщения текстом для модели."""
        content = message.content
        if isinstance(content, str):
            return content

        return str(content)

    @staticmethod
    def _shown_text(message: BaseMessage) -> str:
        """Содержимое сообщения текстом для ленты: у сообщения из блоков
        склеиваются текстовые."""
        content = message.content
        if isinstance(content, str):
            return content

        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue

            if block.get("type") != "text":
                continue

            parts.append(str(block.get("text", "")))

        return "".join(parts)

    @staticmethod
    def _attachments(message: HumanMessage) -> Iterator[Attachment]:
        raw = message.additional_kwargs.get(ResponseField.ATTACHMENTS.value)
        if not isinstance(raw, list):
            return

        for item in raw:
            if not isinstance(item, Mapping):
                continue

            name = item.get(AttachmentField.NAME.value)
            if name is None:
                continue

            path = item.get(AttachmentField.PATH.value)
            if path is None:
                continue

            yield Attachment(name=str(name), path=str(path))

    @staticmethod
    def _calls(message: AIMessage) -> Iterator[DagNode]:
        """Вызовы инструментов сообщения узлами; вызов без идентификатора
        или имени пропускается: шаг ленты и итог ему не адресовать."""
        for call in message.tool_calls:
            call_id = call.get("id")
            if not call_id:
                continue

            name = call.get("name")
            if not name:
                continue

            args = call.get("args")
            if not isinstance(args, Mapping):
                args = {}

            yield DagNode(key=str(call_id), tool=str(name), args=args)

    @staticmethod
    def _usage(message: AIMessage) -> LlmUsage:
        """Расход токенов из ответа модели; без учёта провайдера — нулевой."""
        usage = message.usage_metadata
        if usage is None:
            return LlmUsage()

        details = usage.get(UsageField.OUTPUT_DETAILS)
        if details is None:
            details = {}

        return LlmUsage(
            input_tokens=usage[UsageField.INPUT],
            output_tokens=usage[UsageField.OUTPUT],
            reasoning_tokens=details.get(UsageField.REASONING, 0),
        )


class ChatModelBridge(BaseChatModel):
    """BaseChatModel поверх ChatModel: конверсия сообщений и событий."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    chat_model: ChatModel
    sampling: Mapping[str, Any] = Field(default_factory=dict)
    model_name: str = ""

    LLM_TYPE: ClassVar[str] = "boba-chat-model"

    @property
    @override
    def _llm_type(self) -> str:
        return self.LLM_TYPE

    @property
    @override
    def _identifying_params(self) -> dict[str, Any]:
        """Имя модели для трасера: он читает его из invocation_params."""
        return {"model": self.model_name}

    @override
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | BaseTool],
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, BaseMessage]:
        declared: list[dict[str, Any]] = []
        for tool in tools:
            declared.append(convert_to_openai_tool(tool))

        return self.bind(tools=declared, **kwargs)

    @override
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        msg = "ChatModelBridge is async-only: use ainvoke/astream"
        raise NotImplementedError(msg)

    @override
    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        request = self._request(messages, stop, kwargs)
        request = request.model_copy(update={"stream": False})

        reply = await self.chat_model.reply(request)

        return ChatResult(
            generations=[ChatGeneration(message=self._final_message(reply))]
        )

    @override
    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        request = self._request(messages, stop, kwargs)

        streamed = False
        async for event in self.chat_model.chat(request):
            if isinstance(event, ChatDelta):
                streamed = True
                chunk = self._delta_chunk(event)
            else:
                chunk = self._final_chunk(event, streamed=streamed)

            if chunk is None:
                continue

            if run_manager is not None:
                await run_manager.on_llm_new_token(
                    str(chunk.message.content), chunk=chunk
                )

            yield chunk

    def _request(
        self,
        messages: Sequence[BaseMessage],
        stop: Sequence[str] | None,
        kwargs: Mapping[str, Any],
    ) -> ChatRequest:
        sampling: Mapping[str, Any] = self.sampling
        if stop:
            sampling = {**sampling, "stop": list(stop)}

        turns: list[ChatTurn] = []
        for message in messages:
            turns.append(self._turn(message))

        return ChatRequest(
            messages=turns,
            tools=self._tools(kwargs.get("tools")),
            sampling=sampling,
        )

    @staticmethod
    def _tools(declared: object) -> list[ToolSpec]:
        if not isinstance(declared, Sequence):
            return []

        specs: list[ToolSpec] = []
        for entry in declared:
            if not isinstance(entry, Mapping):
                continue

            function = entry.get("function")
            if not isinstance(function, Mapping):
                continue

            parameters = function.get("parameters")
            if not isinstance(parameters, Mapping):
                parameters = {}

            specs.append(
                ToolSpec(
                    name=str(function.get("name", "")),
                    description=str(function.get("description", "")),
                    parameters=dict(parameters),
                )
            )

        return specs

    @classmethod
    def _turn(cls, message: BaseMessage) -> ChatTurn:
        match message:
            case SystemMessage():
                return ChatTurn(role=ChatRole.SYSTEM, content=cls._text(message))
            case HumanMessage():
                return ChatTurn(role=ChatRole.USER, content=cls._text(message))
            case ToolMessage():
                return ChatTurn(
                    role=ChatRole.TOOL,
                    content=cls._text(message),
                    tool_call_id=message.tool_call_id,
                )
            case AIMessage():
                return cls._assistant_turn(message)
            case _:
                return ChatTurn(role=ChatRole.USER, content=cls._text(message))

    @classmethod
    def _assistant_turn(cls, message: AIMessage) -> ChatTurn:
        # отсутствие ключа и пустая строка — разные вещи: пустую провайдер в
        # режиме размышления требует вернуть, без ключа поле не отправляется
        reasoning: str | None = None
        if ResponseField.REASONING_CONTENT.value in message.additional_kwargs:
            reasoning = str(
                message.additional_kwargs[ResponseField.REASONING_CONTENT.value]
            )

        calls: list[ToolCall] = []
        for call in message.tool_calls:
            call_id = call.get("id")
            if not call_id:
                call_id = ""

            calls.append(
                ToolCall(
                    id=call_id,
                    name=call["name"],
                    arguments=dict(call["args"]),
                )
            )

        return ChatTurn(
            role=ChatRole.ASSISTANT,
            content=cls._text(message),
            reasoning=reasoning,
            tool_calls=calls,
        )

    @staticmethod
    def _text(message: BaseMessage) -> str:
        content = message.content
        if isinstance(content, str):
            return content

        return str(content)

    @staticmethod
    def _delta_chunk(delta: ChatDelta) -> ChatGenerationChunk | None:
        marks: dict[str, Any] = {}
        if delta.reasoning:
            marks[ResponseField.REASONING_CONTENT.value] = delta.reasoning

        message = AIMessageChunk(content=delta.content, additional_kwargs=marks)
        return ChatGenerationChunk(message=message)

    @classmethod
    def _final_chunk(
        cls, reply: ChatReply, *, streamed: bool
    ) -> ChatGenerationChunk | None:
        """Финал потока: вызовы инструментов, а без дельт — и весь ответ."""
        content = ""
        marks: dict[str, Any] = {}
        if not streamed:
            content = reply.content
            if reply.reasoning:
                marks[ResponseField.REASONING_CONTENT.value] = reply.reasoning

        chunks = []
        for index, call in enumerate(reply.tool_calls):
            chunks.append(
                tool_call_chunk(
                    name=call.name,
                    args=json.dumps(dict(call.arguments), ensure_ascii=False),
                    id=call.id,
                    index=index,
                )
            )

        usage = cls._usage(reply)
        if not content and not marks and not chunks and usage is None:
            return None

        message = AIMessageChunk(
            content=content,
            additional_kwargs=marks,
            tool_call_chunks=chunks,
            usage_metadata=usage,
        )
        return ChatGenerationChunk(message=message)

    @classmethod
    def _final_message(cls, reply: ChatReply) -> AIMessage:
        marks: dict[str, Any] = {}
        if reply.reasoning:
            marks[ResponseField.REASONING_CONTENT.value] = reply.reasoning

        calls: list[LangchainToolCall] = []
        for call in reply.tool_calls:
            calls.append(
                LangchainToolCall(
                    name=call.name,
                    args=dict(call.arguments),
                    id=call.id,
                    type="tool_call",
                )
            )

        return AIMessage(
            content=reply.content,
            additional_kwargs=marks,
            tool_calls=calls,
            usage_metadata=cls._usage(reply),
        )

    @staticmethod
    def _usage(reply: ChatReply) -> UsageMetadata | None:
        """Учёт токенов финала; None — провайдер учёт не прислал."""
        usage = reply.usage
        if not usage.input_tokens and not usage.output_tokens:
            return None

        metadata = UsageMetadata(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.input_tokens + usage.output_tokens,
        )

        if usage.reasoning_tokens:
            metadata["output_token_details"] = OutputTokenDetails(
                reasoning=usage.reasoning_tokens
            )

        return metadata
