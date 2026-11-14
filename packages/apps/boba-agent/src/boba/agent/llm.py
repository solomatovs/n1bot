"""Готовый запрос из SQL передаётся в boba-llm-http; ответ становится историей.

Ошибки:
LlmError — провайдер недоступен или нарушил контракт ответа.
SettingsError — некорректные настройки подключения к провайдеру.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from boba.agent.models import ReasoningPart, TextPart, TokenUsage, ToolCallPart
from boba.agent.settings import NamedProfile, ProviderSettings
from boba.llm.chat import LlmOutputLimitError, LlmServiceError, LlmTrouble, ToolCall
from boba.llm.http.openai import (
    OpenAiBackend,
    OpenAiCompletion,
    OpenAiProvider,
    StreamAssembly,
)

Emit = Callable[[int, str, str], Awaitable[None]]


class Generation:
    """Переводит ответ общего OpenAI-компонента в части истории для Agent."""

    def __init__(self) -> None:
        self.response = StreamAssembly("OpenAI response")

    @property
    def finish_reason(self) -> str:
        return self.response.finish_reason

    @property
    def status(self) -> int | None:
        return self.response.status

    @status.setter
    def status(self, value: int | None) -> None:
        self.response.status = value

    @property
    def has_content(self) -> bool:
        return self.response.has_content

    @property
    def usage(self) -> TokenUsage | None:
        usage = self.response.usage
        if usage is None:
            return None

        return TokenUsage(
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            reasoning_tokens=usage.completion_tokens_details.reasoning_tokens,
            cache_read_tokens=usage.prompt_tokens_details.cached_tokens,
        )

    def content(
        self, *, partial: bool = False
    ) -> tuple[TextPart | ReasoningPart | ToolCallPart, ...]:
        return tuple(self._parts(partial=partial))

    def _parts(
        self, *, partial: bool
    ) -> Iterator[TextPart | ReasoningPart | ToolCallPart]:
        for part in self.response.parts(partial=partial):
            if isinstance(part, ToolCall):
                yield ToolCallPart(
                    call_id=part.id, name=part.name, arguments=dict(part.arguments)
                )
                continue

            if part.kind == "reasoning":
                yield ReasoningPart(text=part.text, signature=None, format=None)
                continue

            yield TextPart(text=part.text)


@dataclass(frozen=True)
class ProviderClient:
    """OpenAiTransport владеет backend; completion использует его соединение."""

    config: ProviderSettings
    backend: OpenAiBackend
    completion: OpenAiCompletion
    address: str


class OpenAiTransport:
    """Передаёт запросы в общие OpenAI-клиенты и отдаёт дельты callback агента."""

    def __init__(self, profiles: Iterable[NamedProfile]) -> None:
        clients = self._connections(profiles)
        self._clients = dict(clients)

    def _connections(
        self, profiles: Iterable[NamedProfile]
    ) -> Iterator[tuple[str, ProviderClient]]:
        for profile in profiles:
            config = profile.settings.provider
            connection = config.connection()
            backend = OpenAiBackend(
                OpenAiProvider(
                    kind="openai", connection=connection, transport=config.transport
                )
            )
            client = ProviderClient(
                config,
                backend,
                backend.completion(),
                str(connection.url_of("chat/completions")),
            )

            yield profile.name, client

    def address(self, provider: str) -> str:
        return self._clients[provider].address

    def retry_delay(
        self, provider: str, failure: BaseException, attempt: int
    ) -> float | None:
        if not isinstance(failure, LlmServiceError):
            return None

        config = self._clients[provider].config
        limit = 1
        if failure.trouble is LlmTrouble.DOWN:
            limit = config.retry_attempts

        if failure.status is not None:
            if failure.status >= HTTPStatus.INTERNAL_SERVER_ERROR:
                limit = config.retry_attempts

            limit = config.retry_statuses.get(failure.status, limit)

        if attempt >= limit:
            return None

        return min(config.retry_backoff_sec * attempt, config.retry_after_max_sec)

    async def generate(
        self, provider: str, request: Mapping[str, Any], output: Generation, emit: Emit
    ) -> None:
        try:
            stream = self._clients[provider].completion.generate(
                request, output.response
            )
            async with aclosing(stream) as deltas:
                async for delta in deltas:
                    await emit(delta.position, delta.kind, delta.text)

        except LlmOutputLimitError:
            # Agent сохраняет уже полученный текст с completion=output_limit.
            return

    async def close(self) -> None:
        for client in self._clients.values():
            await client.backend.aclose()
