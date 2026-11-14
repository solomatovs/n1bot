"""Конфигурация сервиса и исходные значения производителей записей истории."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from boba.access import RoleVisibility
from boba.auth.assembly import AuthBackends
from boba.auth.config import AuthConfig
from boba.auth.providers.backends import StandardAuthBackends
from boba.auth.settings import SignInSettings
from boba.config import ConfigVariant
from boba.db.postgres.connection import PostgresConfig
from boba.http_server import ProcessLogging, ServerConfig
from boba.identity.service import ServiceAddress
from boba.oauth.server.clients import TrustedClient
from boba.oauth.server.config import TokenConfig
from boba.transport.http import HttpTransportConfig
from boba.transport.http.connection import HttpConnection, WebAuth, UrlScheme


class SettingsError(ValueError):
    """Недостаточно настроек для запуска агента."""


class SettingsModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, alias_generator=to_camel
    )


class ProviderSettings(SettingsModel):
    kind: Literal["openai"] = "openai"
    base_url: str
    auth: WebAuth
    timeout_sec: float = Field(default=120, gt=0)
    ssl_verify: bool = True
    retry_attempts: int = Field(default=1, ge=1)
    retry_backoff_sec: float = Field(default=1, ge=0)
    retry_statuses: dict[int, int] = Field(default_factory=dict)
    retry_after_max_sec: float = Field(default=60, ge=0)
    transport: HttpTransportConfig = Field(default_factory=HttpTransportConfig)

    def connection(self) -> HttpConnection:
        url = urlsplit(self.base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.query
            or url.fragment
        ):
            raise SettingsError(
                "Provider URL must be an HTTP base URL without credentials or query"
            )
        # Retry belongs to the agent so that every attempt is recorded in history.
        return HttpConnection(
            scheme=UrlScheme(url.scheme),
            host=url.hostname,
            port=url.port,
            path=url.path,
            auth=self.auth,
            timeout_sec=self.timeout_sec,
            ssl_verify=self.ssl_verify,
            retry_attempts=1,
        )


class ModelSettings(SettingsModel):
    provider: ProviderSettings
    model: str = Field(min_length=1)
    sampling: dict[str, Any] = Field(default_factory=dict)
    inference: dict[str, Any] = Field(default_factory=dict)
    provider_parameters: dict[str, Any] = Field(default_factory=dict)
    system_prompt: str | None = None
    context_window: int = Field(default=128000, gt=0)
    max_output_tokens: int = Field(default=4096, gt=0)
    reasoning_return: Literal["never", "turn", "always"] = "turn"


class PermissionSettings(SettingsModel):
    default_mode: Literal[
        "default", "acceptEdits", "plan", "dontAsk", "bypassPermissions", "auto"
    ] = "default"
    allow_bypass: bool = False
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


class Limits(SettingsModel):
    max_turns: int = Field(default=32, gt=0)
    tool_result_max_chars: int = Field(default=100000, gt=0)
    tool_result_window_share: float = Field(default=0.25, gt=0, le=1)


class McpSettings(SettingsModel):
    list_timeout_sec: float = Field(default=30, gt=0)
    call_timeout_sec: float = Field(default=600, gt=0)
    input_rounds: int = Field(default=3, gt=0)


class ToolsSettings(SettingsModel):
    always_load: tuple[str, ...] = ()


class SessionsSettings(SettingsModel):
    max_open: int = Field(default=256, gt=0)
    subscriber_buffer: int = Field(default=256, gt=0)


class SettingsLayer(SettingsModel):
    permissions: PermissionSettings = Field(default_factory=PermissionSettings)
    limits: Limits = Field(default_factory=Limits)
    mcp: McpSettings = Field(default_factory=McpSettings)
    tools: ToolsSettings = Field(default_factory=ToolsSettings)
    sessions: SessionsSettings = Field(default_factory=SessionsSettings)


class DataConfig(BaseModel):
    """Каталог данных экземпляра; имена подкаталогов передаёт потребитель."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    SECTION: ClassVar[str] = "data"

    data_dir: str = Field(
        min_length=1,
        description="Абсолютный путь к данным экземпляра приложения.",
    )

    @model_validator(mode="before")
    @classmethod
    def _directory(cls, value: object) -> object:
        if isinstance(value, str):
            return {"data_dir": value}

        return value

    @field_validator("data_dir")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not os.path.isabs(value):
            msg = f"data: expected an absolute path, got {value!r}"
            raise ValueError(msg)

        return os.path.normpath(value)

    def data(self, part: str) -> Path:
        return Path(self.data_dir) / part


class TlsConfig(BaseModel):
    """Доверенные CA исходящих подключений процесса и сетевых песочниц."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    SECTION: ClassVar[str] = "tls"
    CA_ENV: ClassVar[str] = "SSL_CERT_FILE"
    ca_file: str = Field(default="/etc/ssl/certs/ca-certificates.crt", min_length=1)

    @field_validator("ca_file")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not os.path.isabs(value):
            raise ValueError("[tls].ca_file must be an absolute path")
        return os.path.normpath(value)


class AgentProfile(RoleVisibility):
    """Профиль агента [agent.profiles.<имя>]: роли, которым он выдан, и
    профиль модели агента — модель, её пределы и свой системный промпт."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: ModelSettings


class ServeHistory(BaseModel):
    """Секция [agent.history]: схема истории и подключение истории
    ([postgres] целиком, со своей ролью и пулом)."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    db_schema: str = Field(alias="schema", min_length=1)
    postgres: PostgresConfig


class ServeSection(BaseModel):
    """Секция [agent]: адрес службы и путь ACP, публичный адрес и префикс
    (ServiceAddress), история, слой настроек
    агента без истории и моделей, профили агента и доверенные клиенты
    OAuth."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(default="/acp", min_length=1, pattern=r"^/")
    idle_sec: float = Field(default=1800, gt=0)
    """Подключение HTTP без запросов и ходов дольше idle_sec закрывается
    вместе с сессиями: клиент ушёл, не закрыв его."""
    settings: SettingsLayer = Field(default_factory=SettingsLayer)
    """Слой настроек агента: права, лимиты, сессии, серверы MCP, промпт.
    Историю, профили моделей и профиль по умолчанию задаёт служба."""
    profiles: dict[str, AgentProfile] = Field(min_length=1)
    clients: dict[str, TrustedClient] = Field(default_factory=dict)
    """Доверенные клиенты OAuth по client_id: им разрешён вход proxy."""


class AgentServerConfig(ServerConfig, ServiceAddress):
    model_config = ConfigDict(extra="forbid", frozen=True)
    port: int = Field(default=8660, gt=0, lt=65536)


class ServeConfig(SignInSettings):
    """Секции службы агента и подключения её инфраструктуры."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    db_schema: str = Field(alias="schema", min_length=1)
    paths: DataConfig = Field(alias="data")
    tls: TlsConfig = Field(default_factory=TlsConfig)
    session: TokenConfig
    logger: dict[str, Any] = Field(default_factory=ProcessLogging().default)

    AUTH_BACKENDS: ClassVar[AuthBackends] = StandardAuthBackends().registry
    CONFIG_VARIANTS: ClassVar = {
        AuthConfig: ConfigVariant("type", AUTH_BACKENDS.model_for),
    }

    server: AgentServerConfig
    postgres: PostgresConfig
    agent: ServeSection

    @property
    def history(self) -> ServeHistory:
        return ServeHistory(schema=self.db_schema, postgres=self.postgres)
