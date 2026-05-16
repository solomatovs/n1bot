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
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)
from boba.patterns import Converter
from boba.provider.openai.visitor import OpenAIChatVisitor
from boba.tools.domain import ToolResultVisitor, ToolSchema
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
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


class ToOpenAIMessageConverter(Converter[Message, ChatCompletionMessageParam]):
    """Конвертация Message-иерархии → OpenAI message param (visitor по типу).

    Для `ToolResultMessage` доменный `ToolResult` рендерится через переданный
    `ToolResultVisitor[str]` — это граница между домен-моделью и wire-форматом
    конкретного провайдера.
    """

    def __init__(
        self,
        tool_result_visitor: ToolResultVisitor[str] | None = None,
    ) -> None:
        self._tool_result_visitor: ToolResultVisitor[str] = (
            tool_result_visitor or OpenAIChatVisitor()
        )

    def convert(self, value: Message) -> ChatCompletionMessageParam:
        """Flatten доменных блоков в OpenAI Chat Completions wire-shape.

        OpenAI Chat не interleav'ит блоки — `content` это одна строка, а
        tool_calls — поле параллельное content. Поэтому: для system/user
        склеиваем все TextBlock; для assistant — текст в content, tool_calls
        в одноимённое поле. Не-text блоки в system/user пока игнорируются
        (image/file требуют отдельной формы content-array — добавим, когда
        мультимодальность станет use-case'ом).
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
            case AssistantMessage(blocks=blocks):
                text_parts: list[str] = []
                tool_calls: list[Any] = []
                for block in blocks:
                    match block:
                        case TextBlock(content=c):
                            text_parts.append(c)
                        case ToolCallBlock(call=tc):
                            tool_calls.append(
                                {
                                    "id": tc.id,
                                    "type": "function",
                                    "function": {
                                        "name": tc.name,
                                        "arguments": tc.args_json(),
                                    },
                                },
                            )
                        case _:
                            # thinking / refusal / invalid_tool_call —
                            # для replay/audit держим в домене, в OpenAI
                            # Chat не отправляем.
                            pass
                param = ChatCompletionAssistantMessageParam(
                    role="assistant",
                    content="".join(text_parts),
                )
                if tool_calls:
                    param["tool_calls"] = tool_calls
                return param
            case ToolResultMessage(tool_call_id=tcid, result=result):
                return ChatCompletionToolMessageParam(
                    role="tool",
                    content=result.accept(self._tool_result_visitor),
                    tool_call_id=tcid,
                )
            case _:
                msg = f"ToOpenAIMessageConverter: неизвестный Message-тип: {type(value).__name__}"
                raise LLMProtocolError(msg)


class ToOpenAIRequestConverter(Converter[LLMRequest, dict[str, Any]]):
    """LLMRequest → kwargs для client.chat.completions.create."""

    def __init__(
        self,
        tool_result_visitor: ToolResultVisitor[str] | None = None,
    ) -> None:
        self._to_message = ToOpenAIMessageConverter(tool_result_visitor)
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
        """Склеивает в OpenAI-порядок: system_messages → messages."""
        kwargs["messages"] = list(
            self._convert_messages(chain(r.system_messages, r.messages))
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
