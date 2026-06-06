"""`PostgresConnection` — переиспользуемая базовая модель Postgres-подключения.

`BaseModel`, без `config_path` — встраивается как nested-поле в tool-конфиги,
которым нужно соединение с Postgres (ingest, search, sql). Заполняется ссылкой
`${postgres.<name>}` из секции `[postgres.<name>]` в config.toml.

Связанные helpers:
- `to_dsn(session_options=...)` — libpq DSN через `psycopg.conninfo.make_conninfo`.
- `to_pool_config()` — `PostgresConfig` для `PostgresPool.get(...)` — drop-in.
"""

from __future__ import annotations

from typing import Any, Self

from psycopg.conninfo import make_conninfo
from pydantic import BaseModel, Field, model_validator

from boba.db.postgres.config import PostgresConfig

__all__ = ["PostgresConnection"]


class PostgresConnection(BaseModel):
    """
    Структурированное Postgres-подключение.
    """

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
            "Пароль Postgres. Обычно из env (`...__PASSWORD`). "
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
        default="boba",
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
    statement_timeout_ms: int = Field(
        default=5000,
        ge=100,
        description="PG statement_timeout (мс)",
    )
    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.host:
            msg = "postgres connection: host обязателен"
            raise ValueError(msg)
        if not self.user:
            msg = "postgres connection: user обязателен"
            raise ValueError(msg)
        if not self.database:
            msg = "postgres connection: database обязателен"
            raise ValueError(msg)
        if self.pool_max_size < self.pool_min_size:
            msg = (
                f"postgres connection: pool_max_size ({self.pool_max_size}) "
                f"должен быть ≥ pool_min_size ({self.pool_min_size})"
            )
            raise ValueError(msg)
        return self

    def to_dsn(self, session_options: dict[str, str] | None = None) -> str:
        """libpq-DSN через `psycopg.conninfo.make_conninfo`.

        `session_options` — словарь GUC, пакуется в libpq-параметр
        `options='-c k=v ...'`. Используется sql-tool'ом для зашивки
        `default_transaction_read_only=on` + `statement_timeout=<ms>`
        прямо в DSN (PG применит при коннекте).
        """
        kwargs: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password or None,
            "dbname": self.database,
            "sslmode": self.sslmode,
            "application_name": self.application_name,
        }
        opts = session_options or {}
        if opts:
            kwargs["options"] = " ".join(f"-c {k}={v}" for k, v in opts.items())
        return make_conninfo(**kwargs)

    def to_pool_config(
        self,
        session_options: dict[str, str] | None = None,
    ) -> PostgresConfig:
        """`PostgresConfig` для `PostgresPool.get(...)` — drop-in."""
        return PostgresConfig(
            dsn=self.to_dsn(session_options=session_options),
            min_size=self.pool_min_size,
            max_size=self.pool_max_size,
            connect_timeout_sec=self.connect_timeout_sec,
        )
