"""Мост ChatModel -> langchain BaseChatModel и чтение сообщений langchain.

Граф хода работает с BaseChatModel; мост конвертирует langchain-сообщения в
конверт ChatRequest, события модели — в чанки и итоговое сообщение. Какой
бэкенд за портом — мосту безразлично. LangchainMessages читает обратно то,
что мост кладёт в additional_kwargs: рассуждения модели, — и переводит итог
вызова инструмента в ToolMessage и обратно.

Ошибки: своих не выпускает; LlmError бэкенда уходит наверх как есть.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar, Literal

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
from pydantic import ConfigDict, Field
from typing_extensions import override

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
    """Ключи additional_kwargs сообщений langchain, которые кладёт мост."""

    REASONING_CONTENT = "reasoning_content"


class LangchainMessages:
    """Перевод между сообщениями langchain и типами чата и ядра.

    Создаётся каждым, кто стоит на стыке с langchain: трасером прогона,
    стыком графа с портом инструментов (LangchainPort), подготовкой хода, сборкой
    ленты из истории и реестром журналов вызовов. Читает сообщение из
    результата генерации и рассуждения, которые мост нормализовал в
    additional_kwargs; итог вызова инструмента переводит в сообщение
    инструмента (tool_message) и обратно (outcome_of) — другого места, где
    итог становится ToolMessage или читается из него, нет.
    """

    JOURNAL_KEY: ClassVar[str] = "boba_journal"
    """Ключ response_metadata сообщения инструмента с адресом журнала вызова."""

    def of_chunk(
        self, chunk: GenerationChunk | ChatGenerationChunk | None
    ) -> BaseMessage | None:
        """Сообщение чанка; у текстовых чанков его нет."""
        if not isinstance(chunk, ChatGenerationChunk):
            return None

        return chunk.message

    def of_result(self, response: LLMResult) -> BaseMessage | None:
        if not response.generations:
            return None

        first = response.generations[0]
        if not first:
            return None

        generation = first[0]
        if not isinstance(generation, ChatGeneration):
            return None

        return generation.message

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
        вызов исполнял не сервер с журналом. От результата в сообщении не
        зависит: адрес несёт и запись без результата семейства.

        Ошибки:
        pydantic.ValidationError — поля адреса журнала не проходят модель.
        """
        stamped = message.response_metadata.get(self.JOURNAL_KEY)
        if not stamped:
            return None

        return JournalAddress.model_validate(stamped)

    @staticmethod
    def text_of(message: BaseMessage) -> str:
        """Содержимое сообщения текстом."""
        content = message.content
        if isinstance(content, str):
            return content

        return str(content)


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
