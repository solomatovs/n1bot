"""PgExecutorConfig + PgExecutor: postgres поверх общего SQL-слоя.

Ошибки: SqlQueryError — запрос не выполнен; UnknownConnectionError — имя
подключения вне whitelist'а; CollectorCapacityError — выгрузка переросла
max_bytes.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

from pydantic import Field

from boba.db.postgres import PostgresConfig
from boba.tool.pg.caller import PgCaller, PgParams
from boba.toolkit.launcher import (
    CollectorCapacityError,
    LauncherError,
    TextCollector,
)
from boba.toolkit.sql import SqlExecutor, SqlProfiles, SqlQueryError

__all__ = [
    "PgExecutor",
    "PgExecutorConfig",
]


class PgExecutorConfig(SqlProfiles[PostgresConfig]):
    """Конфиг для PgExecutor."""

    SECTION: ClassVar[str] = "tool.pg"

    profiles: dict[str, PostgresConfig] = Field(
        default_factory=dict,
        description=(
            "dict[connection_name, postgres-профиль ссылкой]: "
            '`[tool.pg.profiles] main = "${postgres.main}"`. '
            "Ключ — значение tool-arg `connection_name` (LLM выбирает БД по нему)."
        ),
    )


class PgExecutor(SqlExecutor[PostgresConfig, PgParams]):
    """Добавляет к общему исполнителю выгрузку COPY.

    Режим сессии берётся из профиля: read-only задаётся параметром
    default_transaction_read_only в [postgres.<name>.options].
    """

    def __init__(self, *, cfg: PgExecutorConfig, caller: PgCaller) -> None:
        super().__init__(cfg=cfg, caller=caller)
        self._pg_caller = caller

    async def execute_copy(self, query: str, *, connection_name: str) -> str:
        """COPY ... TO STDOUT: текст собирается целиком, потолок по байтам."""
        collector = TextCollector(
            max_chars=self.max_bytes,
            limit_rows=self.max_rows_cap,
            header_lines=1,
        )
        connection = self.connection_of(connection_name)
        try:
            trailer = await asyncio.to_thread(
                self._pg_caller.copy,
                connection=connection,
                sql=query,
                max_bytes=self.max_bytes,
                sink=collector,
            )
        except LauncherError as e:
            msg = f"SQL copy failed (connection_name={connection_name!r}): {e}"
            raise SqlQueryError(msg) from e

        if trailer.truncated:
            msg = f"pg copy: stream exceeded max_bytes {self.max_bytes}"
            raise CollectorCapacityError(msg)
        return collector.text()
