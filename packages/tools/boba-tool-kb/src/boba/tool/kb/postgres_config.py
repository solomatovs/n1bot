"""`PostgresConnectionConfig` — структурированное подключение к Postgres.

Секция `[tool.kb.postgres]`. Связное подключение задаётся отдельными
полями (host/port/user/password/database/sslmode/...); итоговый libpq-DSN
собирается через `psycopg.conninfo.make_conninfo` — безопасно для
символов спецсимволов в паролях и query-параметрах.

Никакой обратной совместимости с raw-`dsn`-строкой: оператор указывает
поля явно, а pipeline-стороны (`PostgresPool`, `external_fts`-pool)
получают DSN через `.to_pool_config()`.
"""

from __future__ import annotations

from typing import Self

from psycopg.conninfo import make_conninfo
from pydantic import Field, model_validator

from boba.db.postgres import PostgresConfig
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict

__all__ = ["PostgresConnectionConfig"]


class PostgresConnectionConfig(BobaFlatSettings):
    """Структурированное подключение к Postgres ([tool.kb.postgres])."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.postgres",
    )

    host: str = Field(
        default="",
        description="Хост Postgres. Обязателен.",
    )
    port: int = Field(
        default=5432,
        ge=1,
        le=65535,
        description="TCP-порт Postgres.",
    )
    user: str = Field(
        default="",
        description="Пользователь Postgres. Обязателен.",
    )
    password: str = Field(
        default="",
        description=(
            "Пароль Postgres. Обычно из env `BOBA_TOOL__KB__POSTGRES__PASSWORD`. "
            "Допустимы спецсимволы — DSN собирается через `make_conninfo`, "
            "URL-encoding не требуется."
        ),
    )
    database: str = Field(
        default="",
        description="Имя базы данных. Обязателен.",
    )
    sslmode: str = Field(
        default="prefer",
        description=(
            "TLS-режим psycopg/libpq: `disable` | `allow` | `prefer` | "
            "`require` | `verify-ca` | `verify-full`."
        ),
    )
    application_name: str = Field(
        default="boba-tool-kb",
        description=(
            "Метка соединения для PG `pg_stat_activity` — удобно "
            "разделять запросы разных приложений."
        ),
    )
    pool_min_size: int = Field(
        default=1,
        ge=0,
        description="Минимальный размер connection-pool'а (psycopg_pool).",
    )
    pool_max_size: int = Field(
        default=8,
        ge=1,
        description="Максимальный размер connection-pool'а.",
    )
    connect_timeout_sec: float = Field(
        default=10.0,
        gt=0,
        description="Таймаут установки соединения (сек).",
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Под discover-flow конфиг грузится только если плагин enabled.

        Значит к load-time host/user/database должны быть валидно заполнены
        (либо через TOML, либо через env).
        """
        if not self.host:
            msg = "kb.postgres.host обязателен"
            raise ValueError(msg)
        if not self.user:
            msg = "kb.postgres.user обязателен"
            raise ValueError(msg)
        if not self.database:
            msg = "kb.postgres.database обязателен"
            raise ValueError(msg)
        if self.pool_max_size < self.pool_min_size:
            msg = (
                f"kb.postgres.pool_max_size ({self.pool_max_size}) "
                f"должен быть ≥ pool_min_size ({self.pool_min_size})"
            )
            raise ValueError(msg)
        return self

    def to_dsn(self) -> str:
        """libpq-DSN через `psycopg.conninfo.make_conninfo`.

        `dbname` — стандартное libpq-имя для базы (не `database`). Пустые
        строковые поля опускаются.
        """
        return make_conninfo(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password or None,
            dbname=self.database,
            sslmode=self.sslmode,
            application_name=self.application_name,
        )

    def to_pool_config(self) -> PostgresConfig:
        """`PostgresConfig` для `PostgresPool.get(...)` — drop-in."""
        return PostgresConfig(
            dsn=self.to_dsn(),
            min_size=self.pool_min_size,
            max_size=self.pool_max_size,
            connect_timeout_sec=self.connect_timeout_sec,
        )
