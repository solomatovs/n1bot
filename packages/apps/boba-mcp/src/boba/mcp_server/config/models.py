"""Модели конфигурации процесса MCP и секций его сервера."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializeAsAny,
    field_validator,
    model_validator,
)

from boba.auth.assembly import AuthBackends
from boba.auth.config import AuthConfig
from boba.auth.providers.backends import StandardAuthBackends
from boba.auth.settings import SignInSettings
from boba.config import ConfigVariant
from boba.crypto.config import ValueCipherConfig
from boba.db.postgres.connection import PostgresConfig
from boba.http_server import ProcessLogging, ServerConfig
from boba.identity.service import ServiceAddress
from boba.mcp_server.storage import StandardStorageBackends
from boba.oauth.server.clients import TrustedClient
from boba.oauth.server.config import TokenConfig
from boba.storage.client import StorageConfig
from boba.toolrun.plugins import PluginSections


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


class RunLimits(BaseModel):
    """Пределы одновременных вызовов инструментов сервиса.

    max_runs — сколько вызовов исполняется разом; max_waiting — сколько
    ещё ждут места в памяти. Вызов сверх обоих получает отказ результатом.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_runs: int = Field(gt=0)
    max_waiting: int = Field(ge=0)


class McpSection(BaseModel):
    """Пределы запусков MCP и доверенные OAuth-клиенты из конфига процесса."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    limits: RunLimits
    clients: dict[str, TrustedClient] = Field(default_factory=dict)
    """Доверенные клиенты OAuth по client_id: им разрешён вход proxy."""


class McpServerConfig(ServerConfig, ServiceAddress):
    model_config = ConfigDict(extra="forbid", frozen=True)
    port: int = Field(default=8650, gt=0, lt=65536)


class McpAppConfig(SignInSettings):
    """Секции процесса сервиса: секции процесса, [mcp], хранилище workspace и
    каталоги соединений и пользователей."""

    db_schema: str = Field(alias="schema", min_length=1)
    plugins: str = ""
    paths: DataConfig = Field(alias="data")
    tls: TlsConfig = Field(default_factory=TlsConfig)
    session: TokenConfig
    logger: dict[str, Any] = Field(default_factory=ProcessLogging().default)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    AUTH_BACKENDS: ClassVar[AuthBackends] = StandardAuthBackends().registry
    CONFIG_FILES: ClassVar = PluginSections.FILE_SECTIONS

    server: McpServerConfig
    mcp: McpSection
    postgres: PostgresConfig
    connection_store: ValueCipherConfig
    """Ключ шифрования секретов каталога MCP, отдельный от подписи токенов."""
    STORAGE_BACKENDS: ClassVar = StandardStorageBackends().registry
    CONFIG_VARIANTS: ClassVar = {
        AuthConfig: ConfigVariant("type", AUTH_BACKENDS.model_for),
        StorageConfig: ConfigVariant("kind", STORAGE_BACKENDS.model_for),
    }

    storage: SerializeAsAny[StorageConfig]
    """Хранилище workspace: туда пишут маршруты файлов и оттуда читают
    инструменты."""

    @field_validator("storage", mode="before")
    @classmethod
    def _storage_backend(cls, raw: object) -> StorageConfig:
        return cls.STORAGE_BACKENDS.parse(raw)
