"""Мост ChatProvider -> langchain BaseChatModel и фабрика чат-бэкендов.

Граф хода работает с BaseChatModel; мост конвертирует langchain-сообщения в
конверт ChatRequest, события провайдера — в чанки и итоговое сообщение.
Какой бэкенд за портом — мосту безразлично. Фабрика собирает реализацию
ChatProvider по union-конфигу: ресурсы (httpx-клиент, локальный рантайм)
передаёт вызывающий — их жизненным циклом владеет DI приложения.

Ошибки: своих не выпускает; ChatProviderError бэкенда уходит наверх как есть.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, ClassVar

import httpx
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
    ToolCall,
    ToolMessage,
)
from langchain_core.messages.ai import UsageMetadata
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict
from typing_extensions import override

from boba.llm.chat import ResponseField
from boba.llm.local import LocalChatProvider, OnnxChatRuntime
from boba.llm.openai_chat import OpenAiChatProvider
from boba.llm.provider import (
    ChatDelta,
    ChatProvider,
    ChatReply,
    ChatRequest,
    ChatRole,
    ChatSampling,
    ChatTurn,
    LocalChatConfig,
    OpenAiChatConfig,
    ToolCallRequest,
    ToolSpec,
)

__all__ = ["ChatProviderFactory", "ProviderChatModel"]


class ChatProviderFactory:
    """Собирает ChatProvider по union-конфигу бэкенда.

    Ресурсы приходят снаружи: httpx-клиент openai-бэкенда и локальный рантайм
    строит и держит DI приложения — фабрика только выбирает реализацию.
    """

    @classmethod
    def build(
        cls,
        cfg: LocalChatConfig | OpenAiChatConfig,
        *,
        model: str,
        client: httpx.AsyncClient | None,
        runtime: OnnxChatRuntime | None,
    ) -> ChatProvider:
        match cfg:
            case LocalChatConfig():
                if runtime is None:
                    msg = f"local chat backend needs a runtime: {cfg.model_dir}"
                    raise ValueError(msg)

                return LocalChatProvider(runtime)
            case OpenAiChatConfig():
                if client is None:
                    msg = "openai chat backend needs an httpx client"
                    raise ValueError(msg)

                return OpenAiChatProvider(cfg, client, model)


class ProviderChatModel(BaseChatModel):
    """BaseChatModel поверх ChatProvider: конверсия сообщений и событий."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    provider: ChatProvider
    sampling: ChatSampling = ChatSampling()
    model_name: str = ""

    LLM_TYPE: ClassVar[str] = "boba-chat-provider"

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
        msg = "ProviderChatModel is async-only: use ainvoke/astream"
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

        reply: ChatReply | None = None
        async for event in self.provider.chat(request):
            if isinstance(event, ChatReply):
                reply = event

        if reply is None:
            reply = ChatReply()

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
        async for event in self.provider.chat(request):
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
        sampling = self.sampling
        if stop:
            sampling = sampling.model_copy(update={"stop": tuple(stop)})

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

        calls: list[ToolCallRequest] = []
        for call in message.tool_calls:
            call_id = call.get("id")
            if not call_id:
                call_id = ""

            calls.append(
                ToolCallRequest(
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

        calls: list[ToolCall] = []
        for call in reply.tool_calls:
            calls.append(
                ToolCall(
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

        return UsageMetadata(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.input_tokens + usage.output_tokens,
        )
