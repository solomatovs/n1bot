"""Конвертация LLMRequest в kwargs для OpenAI-совместимого API."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from boba_next.llm.models import LLMMessage, LLMRequest, LLMToolSchema
from boba.patterns import Converter
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionToolParam,
    ChatCompletionUserMessageParam,
)


class ToOpenAIToolConverter(Converter[LLMToolSchema, ChatCompletionToolParam]):
    """Конвертация LLMToolSchema → OpenAI tools."""

    def convert(self, value: LLMToolSchema) -> ChatCompletionToolParam:
        return {
            "type": "function",
            "function": {
                "name": value.name,
                "description": value.description,
                "parameters": dict(value.parameters_schema),
            },
        }


class ToOpenAIMessageConverter(Converter[LLMMessage, ChatCompletionMessageParam]):
    """Конвертация LLMMessage → OpenAI message param."""

    def convert(self, value: LLMMessage) -> ChatCompletionMessageParam:
        match value.role:
            case "system":
                return ChatCompletionSystemMessageParam(
                    role="system",
                    content=value.content,
                )
            case "user":
                return ChatCompletionUserMessageParam(
                    role="user",
                    content=value.content,
                )
            case "assistant":
                param = ChatCompletionAssistantMessageParam(
                    role="assistant",
                    content=value.content,
                )
                if value.tool_calls:
                    param["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": tc.arguments,
                            },
                        }
                        for tc in value.tool_calls
                    ]
                return param
            case "tool":
                return ChatCompletionToolMessageParam(
                    role="tool",
                    content=value.content,
                    tool_call_id=value.tool_call_id or "",
                )


class ToOpenAIRequestConverter(Converter[LLMRequest, dict[str, Any]]):
    """LLMRequest → kwargs для client.chat.completions.create."""

    def __init__(self) -> None:
        self._to_message = ToOpenAIMessageConverter()
        self._to_tool = ToOpenAIToolConverter()

    def convert(self, value: LLMRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"stream": True}
        self._apply_model(kwargs, value)
        self._apply_messages(kwargs, value)
        self._apply_sampling(kwargs, value)
        self._apply_tools(kwargs, value)
        self._apply_response_format(kwargs, value)
        return kwargs

    def _apply_model(self, kwargs: dict[str, Any], r: LLMRequest) -> None:
        kwargs["model"] = r.model

    def _apply_messages(self, kwargs: dict[str, Any], r: LLMRequest) -> None:
        """Склеивает в OpenAI-порядок: system → messages."""
        ordered: list[LLMMessage] = [r.system_message, *r.messages]
        kwargs["messages"] = list(self._convert_messages(ordered))

    def _convert_messages(
        self, messages: Iterable[LLMMessage]
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
        t = r.tools
        if t.tools:
            kwargs["tools"] = [self._to_tool.convert(tool) for tool in t.tools]
        if t.tool_choice is not None:
            kwargs["tool_choice"] = t.tool_choice
        if t.parallel_tool_calls is not None:
            kwargs["parallel_tool_calls"] = t.parallel_tool_calls

    def _apply_response_format(self, kwargs: dict[str, Any], r: LLMRequest) -> None:
        if r.response_format is not None:
            kwargs["response_format"] = dict(r.response_format)
