"""`SqlConfig` — конфиг секции `[tool.kb.sql]`.

Отдельная PG-секция для `sql_query` / `sql_list_tables` /
`sql_describe_table`-tool'ов. Не реюзит `[tool.kb.postgres]`, чтобы
безопасность была явной: оператор обычно даёт LLM ограниченную read-only
роль (`GRANT SELECT ON ...`) — это другой DSN, чем у kb_search/ingest.

Поля connection (host/port/user/...) приходят от `PostgresConnectionConfig`
через subclass + override `config_path`. Sql-специфичные limits добавлены
здесь.

Open-mode: whitelist таблиц не декларируется на уровне конфига — LLM
ходит везде, где DSN-роль позволяет. Защита от DROP/INSERT/UPDATE/...
лежит на (1) read-only privileges роли, (2) libpq-параметре `options=`
с `default_transaction_read_only=on` + `statement_timeout=<ms>` — PG
применяет их при коннекте, ни `SET`-statement, ни `configure`-callback
не нужны.
"""

from __future__ import annotations

from pydantic import Field

from boba.settings import BobaSettingsConfigDict
from boba.tool.kb.core.postgres_config import PostgresConnectionConfig

__all__ = ["SqlConfig"]


class SqlConfig(PostgresConnectionConfig):
    """Connection + safety-limits для sql_query tool'ов."""

    # Override section: [tool.kb.sql] вместо [tool.kb.postgres].
    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.sql",
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
