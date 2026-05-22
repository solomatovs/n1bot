"""`SqlConfig` — конфиг секции `[tool.kb.sql]`.

Отдельная PG-секция для `sql_query` / `sql_list_tables` /
`sql_describe_table`-tool'ов. Не реюзит `[tool.kb.postgres_store]`, чтобы
безопасность была явной: оператор обычно даёт LLM ограниченную read-only
роль (`GRANT SELECT ON ...`) — это другой DSN, чем у kb_search/ingest.

Connection-параметры (host/port/user/...) живут в nested-секции
`[tool.kb.sql.connection]` как `PostgresConnectionConfig` (та же модель, что
у `[tool.kb.postgres_store.connection]`). Sql-специфичные limits и опции —
прямо в корневой секции `[tool.kb.sql]`.

Open-mode: whitelist таблиц не декларируется на уровне конфига — LLM
ходит везде, где DSN-роль позволяет. Защита от DROP/INSERT/UPDATE/...
лежит на (1) read-only privileges роли, (2) libpq-параметре `options=`
с `default_transaction_read_only=on` + `statement_timeout=<ms>` — PG
применяет их при коннекте, ни `SET`-statement, ни `configure`-callback
не нужны.
"""

from __future__ import annotations

from typing import Any

from psycopg.conninfo import make_conninfo
from pydantic import Field

from boba.db.postgres import PostgresConfig
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.core.postgres_store import PostgresConnectionConfig

__all__ = ["SqlConfig"]


class SqlConfig(BobaFlatSettings):
    """Connection + safety-limits для sql_query tool'ов.

    Config-секция: `[tool.kb.sql]`. Connection — в nested-секции
    `[tool.kb.sql.connection]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.sql",
    )

    connection: PostgresConnectionConfig = Field(
        description=(
            "Postgres connection (host/port/user/password/database/...) "
            "для read-only DSN. Nested-секция `[tool.kb.sql.connection]`."
        ),
    )
    max_rows: int = Field(
        default=100,
        ge=1,
        description=(
            "Жёсткий потолок числа строк в результате `sql_query`. LLM "
            "может попросить меньше через `row_limit`, но не больше. Cap "
            "применяется через `cur.fetchmany`, не через LIMIT в SQL."
        ),
    )
    max_cell_chars: int = Field(
        default=200,
        ge=1,
        description=(
            "Максимальная длина одного значения cell в markdown-таблице. "
            "Длинные строки обрезаются до `max_cell_chars` с суффиксом '…'. "
            "Защищает payload от blob'ов и многострочного content'а."
        ),
    )
    statement_timeout_ms: int = Field(
        default=5000,
        ge=100,
        description=(
            "PG statement_timeout (мс) — зашивается в DSN через libpq "
            "параметр `options='-c statement_timeout=<ms>'`. PG применяет "
            "при коннекте, действует на ВСЕ statement'ы сессии."
        ),
    )

    def session_options(self) -> dict[str, str]:
        """Session-level GUC, зашиваемые в `options=`-параметр DSN.

        - `default_transaction_read_only=on` — каждый BEGIN стартует в
          read-only-режиме; PG отклоняет INSERT/UPDATE/DELETE/COPY как
          `cannot execute X in a read-only transaction`. DDL (DROP/CREATE)
          блокируется только правами роли.
        - `statement_timeout=<ms>` — runtime-cap на длительность.
        """
        return {
            "default_transaction_read_only": "on",
            "statement_timeout": str(self.statement_timeout_ms),
        }

    def to_dsn(self) -> str:
        """libpq-DSN из `self.connection` + sql-специфичные `session_options`.

        Не делегирует `connection.to_dsn()`, потому что у того опции пустые;
        здесь сразу включаем read-only / statement_timeout через `options=`.
        """
        conn = self.connection
        kwargs: dict[str, Any] = {
            "host": conn.host,
            "port": conn.port,
            "user": conn.user,
            "password": conn.password or None,
            "dbname": conn.database,
            "sslmode": conn.sslmode,
            "application_name": conn.application_name,
        }
        opts = self.session_options()
        if opts:
            kwargs["options"] = " ".join(f"-c {k}={v}" for k, v in opts.items())
        return make_conninfo(**kwargs)

    def to_pool_config(self) -> PostgresConfig:
        """`PostgresConfig` для `PostgresPool.get(...)` — drop-in."""
        conn = self.connection
        return PostgresConfig(
            dsn=self.to_dsn(),
            min_size=conn.pool_min_size,
            max_size=conn.pool_max_size,
            connect_timeout_sec=conn.connect_timeout_sec,
        )
