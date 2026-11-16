"""Готовый запрос из SQL передаётся в boba-llm-http; ответ становится историей.

Ошибки:
LlmError — провайдер недоступен или нарушил контракт ответа.
SettingsError — некорректные настройки подключения к провайдеру.
"""

from __future__ import annotations

from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
)
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, replace
from http import HTTPStatus
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, TypeAdapter

from boba.agent.history import HistoryStore
from boba.agent.models import ReasoningPart, TextPart, TokenUsage, ToolCallReferencePart
from boba.agent.queries import Query
from boba.agent.settings import ProviderSettings, SettingsError
from boba.crypto import SecretCipher, SecretCryptoError
from boba.llm.chat import LlmOutputLimitError, LlmServiceError, LlmTrouble, ToolCall
from boba.llm.http import LlmEndpoint, LlmRoute
from boba.llm.http.openai import (
    OpenAiCompletion,
    StreamAssembly,
)
from boba.transport.http import (
    HttpRequest,
    HttpResponse,
    HttpTransport,
    HttpTransportConfig,
)
from boba.transport.http.connection import HttpConnection, NoneAuth

Emit = Callable[[int, str, str], Awaitable[None]]


class Generation:
    """Переводит ответ общего OpenAI-компонента в части истории для Agent."""

    def __init__(self) -> None:
        self.response = StreamAssembly("OpenAI response")
        self.address: str | None = None
        self.settings: ProviderSettings | None = None

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
    ) -> tuple[TextPart | ReasoningPart | ToolCallReferencePart, ...]:
        return tuple(self._parts(partial=partial))

    def calls(self) -> tuple[ToolCall, ...]:
        calls = []
        for part in self.response.parts(partial=False):
            if isinstance(part, ToolCall):
                calls.append(part)

        return tuple(calls)

    def _parts(
        self, *, partial: bool
    ) -> Iterator[TextPart | ReasoningPart | ToolCallReferencePart]:
        for part in self.response.parts(partial=partial):
            if isinstance(part, ToolCall):
                if partial:
                    continue

                yield ToolCallReferencePart(call_id=part.id)
                continue

            if part.kind == "reasoning":
                yield ReasoningPart(text=part.text, signature=None, format=None)
                continue

            yield TextPart(text=part.text)


@dataclass(frozen=True)
class ProviderClient:
    """OpenAiTransport владеет HTTP-транспортом; completion разбирает ответы."""

    transport: HttpTransport
    completion: OpenAiCompletion
    address: str


class ProviderHttpTransport(HttpTransport):
    """Добавляет заголовки подключения к HttpRequest перед штатным HTTP-обменом.

    OpenAiTransport передаёт адаптер в общий LlmEndpoint и владеет соединением.
    """

    def __init__(
        self,
        connection: HttpConnection,
        config: HttpTransportConfig,
        headers: Mapping[str, SecretStr],
    ) -> None:
        super().__init__(connection, config)
        self._headers = headers

    @asynccontextmanager
    async def fetch(self, request: HttpRequest) -> AsyncGenerator[HttpResponse, None]:
        headers = {}
        for name, value in self._headers.items():
            headers[name] = value.get_secret_value()

        headers.update(request.headers)
        prepared = replace(request, headers=headers)
        async with super().fetch(prepared) as response:
            yield response


class ProviderConfiguration(BaseModel):
    """Конфигурация подключения; SecretCipher шифрует секретные поля для PostgreSQL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    settings: ProviderSettings
    headers: dict[str, SecretStr] = Field(repr=False)


class ProviderRow(BaseModel):
    """Строка реестра PostgreSQL с зашифрованными секретными полями JSONB."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    configuration: dict[str, JsonValue] = Field(repr=False)


class Providers:
    """Личные подключения пользователей в PostgreSQL без общих провайдеров.

    Сборка службы передаёт объект в SessionHub; ACP изменяет настройки,
    UserInference читает настройки перед отправкой запроса.
    """

    def __init__(self, store: HistoryStore, cipher: SecretCipher) -> None:
        self.store = store
        self.cipher = cipher
        self._headers = TypeAdapter(dict[str, SecretStr])

    async def choices(self, owner: str) -> tuple[str, ...]:
        rows = await self.store.rows(Query.PROVIDER_LIST, owner)
        choices = []
        for row in rows:
            choices.append(row["provider_id"])

        return tuple(choices)

    async def save(
        self, configuration: ProviderConfiguration, *, enabled: bool
    ) -> None:
        encrypted = self.cipher.encrypt(configuration)
        await self.store.rows(
            Query.PROVIDER_SET,
            configuration.owner_id,
            configuration.provider_id,
            configuration.settings.base_url,
            Jsonb(encrypted),
            enabled,
        )

    async def set(
        self,
        owner: str,
        provider: str,
        base_url: str,
        headers: Mapping[str, str],
    ) -> None:
        settings = ProviderSettings(base_url=base_url, auth=NoneAuth(method="none"))
        settings.connection()
        configuration = ProviderConfiguration(
            owner_id=owner,
            provider_id=provider,
            settings=settings,
            headers=self._headers.validate_python(headers),
        )
        await self.save(configuration, enabled=True)

    async def disable(self, owner: str, provider: str) -> None:
        await self.store.rows(Query.PROVIDER_DISABLE, owner, provider)

    async def configuration(self, owner: str, provider: str) -> ProviderConfiguration:
        rows = await self.store.rows(Query.PROVIDER_GET, owner, provider)
        if not rows:
            raise SettingsError(f"Provider {provider!r} is not registered")

        route = ProviderRow.model_validate(rows[0])
        if not route.enabled:
            raise SettingsError(f"Provider {provider!r} is disabled for this user")

        try:
            raw = self.cipher.decrypt(route.configuration)

        except SecretCryptoError as exc:
            raise SettingsError(f"Cannot decrypt provider {provider!r}: {exc}") from exc

        configuration = ProviderConfiguration.model_validate(raw)

        return configuration


class OpenAiTransport:
    """Передаёт запросы в общие OpenAI-клиенты и отдаёт дельты callback агента."""

    def client(
        self, config: ProviderSettings, headers: Mapping[str, SecretStr] | None = None
    ) -> ProviderClient:
        connection = config.connection()
        request_headers = headers
        if request_headers is None:
            request_headers = {}

        transport = ProviderHttpTransport(connection, config.transport, request_headers)
        endpoint = LlmEndpoint(
            transport, connection, LlmRoute.CHAT_COMPLETIONS, "openai chat"
        )

        return ProviderClient(
            transport,
            OpenAiCompletion(endpoint),
            str(connection.url_of("chat/completions")),
        )

    def for_user(self, owner: str, providers: Providers) -> UserInference:
        return UserInference(self, providers, owner)

    def retry_delay(
        self, output: Generation, failure: BaseException, attempt: int
    ) -> float | None:
        if not isinstance(failure, LlmServiceError):
            return None

        config = output.settings
        if config is None:
            return None

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

    async def exchange(
        self,
        client: ProviderClient,
        request: Mapping[str, Any],
        output: Generation,
        emit: Emit,
    ) -> None:
        output.address = client.address
        try:
            stream = client.completion.generate(request, output.response)
            async with aclosing(stream) as deltas:
                async for delta in deltas:
                    await emit(delta.position, delta.kind, delta.text)

        except LlmOutputLimitError:
            # Agent сохраняет уже полученный текст с completion=output_limit.
            return


class UserInference:
    """Отправляет готовое SQL-тело через настройки владельца сессии.

    SessionHub создаёт объект для Agent; общий OpenAiTransport выполняет HTTP.
    Изменение адреса не закрывает уже выполняющийся запрос другого диалога.
    """

    def __init__(
        self, transport: OpenAiTransport, providers: Providers, owner: str
    ) -> None:
        self.transport = transport
        self.providers = providers
        self.owner = owner

    def retry_delay(
        self, output: Generation, failure: BaseException, attempt: int
    ) -> float | None:
        return self.transport.retry_delay(output, failure, attempt)

    async def generate(
        self, provider: str, request: Mapping[str, Any], output: Generation, emit: Emit
    ) -> None:
        configuration = await self.providers.configuration(self.owner, provider)
        output.settings = configuration.settings
        client = self.transport.client(configuration.settings, configuration.headers)
        try:
            await self.transport.exchange(client, request, output, emit)

        finally:
            await client.transport.close()
