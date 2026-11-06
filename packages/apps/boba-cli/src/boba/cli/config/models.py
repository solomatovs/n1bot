"""Модели конфигурации службы boba serve и её секций."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from boba.access import RoleVisibility
from boba.auth.assembly import AuthBackends
from boba.auth.config import AuthConfig
from boba.auth.providers.backends import StandardAuthBackends
from boba.auth.settings import SignInSettings
from boba.cli.config.settings import (
    ModelSettings,
    SettingsLayer,
)
from boba.config import ConfigVariant
from boba.db.postgres.connection import PostgresConfig
from boba.http_server import (
    ProcessLogging,
    ServerConfig,
)
from boba.identity.service import ServiceAddress
from boba.oauth.server.clients import TrustedClient
from boba.oauth.server.config import TokenConfig


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

    @field_validator("settings")
    @classmethod
    def _service_layer(cls, value: SettingsLayer) -> SettingsLayer:
        if value.history is not None:
            msg = (
                "[agent.settings]: the history is [agent.history] of the service, "
                f"remove history of kind {value.history.kind!r}"
            )
            raise ValueError(msg)

        if value.models:
            msg = (
                "[agent.settings]: model profiles live in [agent.profiles.<name>], "
                f"remove models {sorted(value.models)}"
            )
            raise ValueError(msg)

        if value.model is not None:
            msg = (
                "[agent.settings]: the default model is the first profile granted "
                f"to the user, remove model = {value.model!r}"
            )
            raise ValueError(msg)

        return value


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
