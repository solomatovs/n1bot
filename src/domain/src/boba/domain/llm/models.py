from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from boba.domain.core.patterns import FactoryMethod, UuId
from boba.domain.core.tools.tool import Tool
from boba.domain.llm.errors import (
    LLMRequestModelNoneError,
    LLMRequestSystemMessageNoneError,
    LLMRequestUserMessageNoneError,
)

LLMRole = Literal["system", "user", "assistant", "tool"]


class RequestId(UuId):
    """
    Идентификатор запроса пользователя, проходящий через всю систему.
    """


@dataclass(frozen=True)
class LLMToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class LLMMessage:
    role: LLMRole
    content: str
    tool_call_id: str | None = None
    tool_calls: tuple[LLMToolCall, ...] = ()


@dataclass(frozen=True)
class SamplingParams:
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    seed: int | None = None
    stop: tuple[str, ...] | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None


@dataclass(frozen=True)
class LLMToolRequest:
    tools: tuple[Tool[Any], ...] = ()
    tool_choice: str | None = None
    parallel_tool_calls: bool | None = None


@dataclass(frozen=True)
class LLMRequest:
    model: str
    system_message: LLMMessage
    user_message: LLMMessage
    history_messages: tuple[LLMMessage, ...] = ()
    tools: LLMToolRequest = field(default_factory=LLMToolRequest)
    sampling: SamplingParams = field(default_factory=SamplingParams)
    response_format: Mapping[str, Any] | None = None

    def messages_count(self):
        """
        Возвращает кол-во сообщений отправляемых в llm
        Это все сообщения истории + system + user prompt
        """
        return len(self.history_messages) + 1 + 1

    def has_tools(self) -> bool:
        return self.tools is not None


class LLMRequestBuilder(FactoryMethod):
    model: str | None = None
    system_message: LLMMessage | None = None
    user_message: LLMMessage | None = None
    history_messages: tuple[LLMMessage, ...] = ()
    tools: tuple[Tool[Any], ...] = ()
    tool_choice: str | None = None
    parallel_tool_calls: bool | None = None
    sampling: SamplingParams = SamplingParams()
    response_format: Mapping[str, Any] | None = None

    def set_system_message(self, content: str) -> None:
        self.system_message = LLMMessage(role="system", content=content)

    def set_user_message(self, content: str) -> None:
        self.user_message = LLMMessage(role="user", content=content)

    def set_history_messages(self, messages: Iterable[LLMMessage]) -> None:
        self.history_messages = tuple(messages)

    def set_tools(self, tools: Iterable[Tool[Any]]) -> None:
        self.tools = tuple(tools)

    def validate(self) -> None:
        if self.model is None:
            raise LLMRequestModelNoneError()
        if self.system_message is None:
            raise LLMRequestSystemMessageNoneError()
        if self.user_message is None:
            raise LLMRequestUserMessageNoneError()

    def build(self) -> LLMRequest:
        if self.model is None:
            raise LLMRequestModelNoneError()
        if self.system_message is None:
            raise LLMRequestSystemMessageNoneError()
        if self.user_message is None:
            raise LLMRequestUserMessageNoneError()
        return LLMRequest(
            model=self.model,
            system_message=self.system_message,
            user_message=self.user_message,
            history_messages=self.history_messages,
            tools=LLMToolRequest(
                tools=self.tools,
                tool_choice=self.tool_choice,
                parallel_tool_calls=self.parallel_tool_calls,
            ),
            sampling=self.sampling,
            response_format=self.response_format,
        )


@dataclass
class LLMContext:
    request: LLMRequest
    request_id: RequestId
    attempt: int = 0
