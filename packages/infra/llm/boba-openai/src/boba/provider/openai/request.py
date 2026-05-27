"""Конвертация LLMRequest в kwargs для OpenAI-совместимого API."""

from __future__ import annotations

from collections.abc import Iterable
from itertools import chain
from typing import Any

from boba.llm.errors import LLMProtocolError
from boba.llm.models import (
    AssistantMessage,
    LLMRequest,
    Message,
    SystemMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from boba.patterns import Converter
from boba.tools.domain import ToolSchema
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionToolParam,
    ChatCompletionUserMessageParam,
)


class ToOpenAIToolConverter(Converter[ToolSchema, ChatCompletionToolParam]):
    """Конвертация ToolSchema → OpenAI tools."""

    def convert(self, value: ToolSchema) -> ChatCompletionToolParam:
        return {
            "type": "function",
            "function": {
                "name": value.name,
                "description": value.description,
                "parameters": dict(value.parameters_schema),
            },
        }


class ToOpenAIToolCallConverter(
    Converter[ToolCall, ChatCompletionMessageFunctionToolCallParam],
):
    """Конвертация ToolCall → OpenAI tool_call dict для assistant-message."""

    def convert(self, value: ToolCall) -> ChatCompletionMessageFunctionToolCallParam:
        return {
            "id": value.id,
            "type": "function",
            "function": {
                "name": value.name,
                "arguments": value.args_json(),
            },
        }


class ToOpenAIMessageConverter(Converter[Message, ChatCompletionMessageParam]):
    """Конвертация Message-иерархии → OpenAI message param.

    Message-модель плоская; конвертер мапит её на wire-формат OpenAI Chat
    Completions (плоский content + параллельное поле tool_calls).
    """

    def __init__(self) -> None:
        self._to_tool_call = ToOpenAIToolCallConverter()

    def convert(self, value: Message) -> ChatCompletionMessageParam:
        """
        Message -> ChatCompletionMessageParam

        в OpenAI `content` это одна строка, а tool_calls — поле параллельное
        content. Поэтому:
            для system/user — берём content как есть.
            для assistant — текст в content, tool_calls в одно поле;
                thinking/refusal/tool_call_decode_failures живут в домене
                (для replay/audit) и в OpenAI Chat не отправляются.
        """
        match value:
            case SystemMessage():
                return ChatCompletionSystemMessageParam(
                    role="system",
                    content=value.content,
                )
            case UserMessage():
                return ChatCompletionUserMessageParam(
                    role="user",
                    content=value.content,
                )
            case AssistantMessage():
                param = ChatCompletionAssistantMessageParam(
                    role="assistant",
                    content=value.content,
                )

                tool_calls = [
                    self._to_tool_call.convert(call) for call in value.tool_calls
                ]
                if tool_calls:
                    param["tool_calls"] = tool_calls

                return param
            case ToolResultMessage(tool_call_id=tcid):
                return ChatCompletionToolMessageParam(
                    role="tool",
                    content=value.content,
                    tool_call_id=tcid,
                )
            case _:
                msg = (
                    "ToOpenAIMessageConverter: неизвестный Message-тип: "
                    f"{type(value).__name__}"
                )
                raise LLMProtocolError(msg)


class ToOpenAIRequestConverter(Converter[LLMRequest, dict[str, Any]]):
    """LLMRequest → kwargs для client.chat.completions.create."""

    def __init__(self) -> None:
        self._to_message = ToOpenAIMessageConverter()
        self._to_tool = ToOpenAIToolConverter()

    def convert(self, value: LLMRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        self._apply_stream(kwargs, value.stream)
        self._apply_model(kwargs, value)
        self._apply_messages(kwargs, value)
        self._apply_sampling(kwargs, value)
        self._apply_tools(kwargs, value)
        self._apply_response_format(kwargs, value)
        return kwargs

    def _apply_stream(self, kwargs: dict[str, Any], stream: bool):
        kwargs["stream"] = stream

    def _apply_model(self, kwargs: dict[str, Any], r: LLMRequest) -> None:
        kwargs["model"] = r.model

    def _apply_messages(self, kwargs: dict[str, Any], r: LLMRequest) -> None:
        """Склеивает в OpenAI-порядок: system_messages → messages."""
        kwargs["messages"] = list(
            self._convert_messages(chain(r.system_messages, r.dialog_messages))
        )

    def _convert_messages(
        self, messages: Iterable[Message]
    ) -> Iterable[ChatCompletionMessageParam]:
        for m in messages:
            yield self._to_message.convert(m)

    def _apply_sampling(self, kwargs: dict[str, Any], r: LLMRequest) -> None:
        s = r.sampling
        fields: dict[str, Any] = {
            "temperature": s.temperature,
            "top_p": s.top_p,
            "max_tokens": s.max_tokens,
            "seed": s.seed,
            "stop": list(s.stop) if s.stop is not None else None,
            "frequency_penalty": s.frequency_penalty,
            "presence_penalty": s.presence_penalty,
        }
        for key, val in fields.items():
            if val is not None:
                kwargs[key] = val

    def _apply_tools(self, kwargs: dict[str, Any], r: LLMRequest) -> None:
        t = r.tools_definition
        if not t.tools:
            return
        kwargs["tools"] = [self._to_tool.convert(tool) for tool in t.tools]
        if t.tool_choice is not None:
            kwargs["tool_choice"] = t.tool_choice
        if t.parallel_tool_calls is not None:
            kwargs["parallel_tool_calls"] = t.parallel_tool_calls

    def _apply_response_format(self, kwargs: dict[str, Any], r: LLMRequest) -> None:
        if r.response_format is not None:
            kwargs["response_format"] = dict(r.response_format)
