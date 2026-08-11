"""PgExecutorConfig + PgExecutor: postgres поверх общего SQL-слоя.

Ошибки: SqlQueryError — запрос не выполнен; UnknownConnectionError — имя
подключения вне whitelist'а; CollectorCapacityError — выгрузка переросла
max_bytes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar

from pydantic import Field

from boba.db.postgres import PostgresConfig
from boba.tool.pg.caller import PgCaller
from boba.tool.pg.protocol import PgCopyFormat
from boba.toolkit.launcher import LauncherError, TextCollector
from boba.toolkit.sql import SqlExecutor, SqlProfiles, SqlQueryError

logger = logging.getLogger(__name__)

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


PgParams = tuple[Any, ...]
"""Позиционные параметры psycopg под плейсхолдеры %s."""


class PgExecutor(SqlExecutor[PostgresConfig, PgParams]):
    """Исполняет SQL в песочнице; каждый вызов — отдельный процесс и соединение.

    Режим сессии берётся из профиля: read-only задаётся параметром
    default_transaction_read_only в [postgres.<name>.options].
    """

    def __init__(self, *, cfg: PgExecutorConfig, caller: PgCaller) -> None:
        super().__init__(cfg=cfg, caller=caller)
        self._pg_caller = caller
        logger.info(
            "PgExecutor opened: targets=%s max_rows=%d max_bytes=%d",
            cfg.targets(),
            cfg.max_rows,
            cfg.max_bytes,
        )

    async def execute_copy(
        self,
        query: str,
        *,
        connection_name: str,
        copy_format: PgCopyFormat,
    ) -> str:
        """COPY ... TO STDOUT: текст собирается целиком, потолок держит коллектор."""
        # whitelist проверяется здесь: имя вне списка — отказ фасада, не стадии
        self.connection_of(connection_name)

        collector = TextCollector(
            max_chars=self.max_bytes,
            limit_rows=self.max_rows_cap,
            header_lines=1,
        )

        try:
            await asyncio.to_thread(
                self._pg_caller.copy,
                connection_name=connection_name,
                sql=query,
                copy_format=copy_format,
                sink=collector,
            )
        except LauncherError as e:
            msg = f"SQL copy failed (connection_name={connection_name!r}): {e}"
            raise SqlQueryError(msg) from e

        return collector.text()
