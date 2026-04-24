"""Конвертация :class:`LLMRequest` в kwargs для OpenAI-совместимого API.

Вход — :class:`LLMRequest` (уже прошедший :meth:`LLMRequest.validate`
в :class:`~boba_2.domain.agent.meat.llm_invoke.LLMInvokeMiddleware`,
так что ``model`` не ``None``). Выход — ``dict[str, Any]``, пригодный
для ``client.chat.completions.create(**kwargs)``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionToolParam,
    ChatCompletionUserMessageParam,
)

from boba.domain.core.patterns import Converter
from boba.domain.core.tools.tool import Tool
from boba_2.domain.llm.models import LLMMessage, LLMRequest


class ToOpenAIToolConverter(Converter[Tool[Any], ChatCompletionToolParam]):
    """Tool → OpenAI tools API entry.

    Описание собирается через
    :meth:`ParamSchema.build_wire_schema` — обход валидатора.
    Дрейфа со схемой валидации быть не может: оба канала читают из
    одного :class:`~boba.domain.core.patterns.Validator`.
    """

    def convert(self, value: Tool[Any]) -> ChatCompletionToolParam:
        definition = value.definition()

        properties: dict[str, dict[str, Any]] = {}
        required: list[str] = []

        for p in definition.input_schema.params:
            wire = p.build_wire_schema()
            properties[p.name] = wire.property
            if wire.required:
                required.append(p.name)

        return {
            "type": "function",
            "function": {
                "name": value.tool_id().to_wire(),
                "description": definition.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }


class ToOpenAIMessageConverter(Converter[LLMMessage, ChatCompletionMessageParam]):
    """Одно :class:`LLMMessage` → OpenAI message param.

    Узкий ``Literal`` на ``role`` в :class:`LLMMessage` гарантирует
    исчерпывающее покрытие match-case; pyright подсветит, если
    появится новое значение.
    """

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
    """Мапит :class:`LLMRequest` в kwargs для
    ``client.chat.completions.create``.

    ``stream=True`` добавляется безусловно — S1 LLM-слой всегда
    стримит; non-stream режим появится как отдельный терминал, если
    потребуется.
    """

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
        # r.model — валидирован как не-None в LLMInvokeMiddleware
        # задолго до этой точки.
        kwargs["model"] = r.model

    def _apply_messages(self, kwargs: dict[str, Any], r: LLMRequest) -> None:
        """Склеивает в OpenAI-порядок: ``system → history → user``.

        :class:`LLMRequest` хранит system/user как одиночные слоты и
        history как коллекцию — провайдер-специфичный порядок
        определяется здесь. Для провайдера, который хочет иной
        порядок (Anthropic: system как top-level поле вне messages),
        будет отдельный конвертер.
        """
        ordered: list[LLMMessage] = []
        if r.system_message is not None:
            ordered.append(r.system_message)
        ordered.extend(r.history_messages)
        if r.user_message is not None:
            ordered.append(r.user_message)
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
        if r.tools:
            kwargs["tools"] = [self._to_tool.convert(t) for t in r.tools]
        if r.tool_choice is not None:
            kwargs["tool_choice"] = r.tool_choice
        if r.parallel_tool_calls is not None:
            kwargs["parallel_tool_calls"] = r.parallel_tool_calls

    def _apply_response_format(self, kwargs: dict[str, Any], r: LLMRequest) -> None:
        if r.response_format is not None:
            kwargs["response_format"] = dict(r.response_format)
