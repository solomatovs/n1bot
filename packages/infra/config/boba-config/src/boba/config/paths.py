"""Настройки каталога данных и доверенных CA.

Ошибки:
ValueError — путь в конфигурации не является абсолютным."""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)


class PathsConfig(BaseModel):
    """Каталог данных экземпляра; имена подкаталогов передаёт потребитель."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    SECTION: ClassVar[str] = "paths"

    data_dir: str = Field(
        min_length=1,
        description="Абсолютный путь к данным экземпляра приложения.",
    )

    @field_validator("data_dir")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not os.path.isabs(value):
            msg = f"[paths]: expected an absolute path, got {value!r}"
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
