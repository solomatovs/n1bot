"""SqlExecutorConfig + SqlExecutor."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.db.postgres import PostgresConfig
from boba.tool.pg.caller import PgCaller
from boba.toolkit.launcher import LauncherError

logger = logging.getLogger(__name__)

__all__ = [
    "SqlExecutor",
    "SqlExecutorConfig",
    "SqlQueryError",
    "SqlResult",
]


class SqlExecutorConfig(BaseModel):
    """Конфиг для SqlExecutor."""

    model_config = ConfigDict(extra="ignore")

    profiles: dict[str, PostgresConfig] = Field(
        default_factory=dict,
        description=(
            "dict[target, postgres-профиль ссылкой]: "
            '`[tool.pg.profiles] main = "${postgres.main}"`. '
            "Ключ — значение tool-arg `target` (LLM выбирает БД по нему)."
        ),
    )
    max_rows: int = Field(
        default=100,
        ge=1,
        description="Ограничение по кол-ву строк.",
    )
    max_bytes: int = Field(
        default=1_000_000,
        ge=1,
        description=(
            "Hardlimit на суммарный размер CSV-результата COPY (байт). "
            f"Default {1_000_000}. Превышение -> ошибка LLM "
            "«добавьте LIMIT»."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.profiles:
            msg = (
                "tool.pg: no profiles configured. Add [postgres.<name>] and "
                'reference it: [tool.pg.profiles] <name> = "${postgres.<name>}".'
            )
            raise ValueError(msg)
        return self

    def targets(self) -> list[str]:
        return sorted(self.profiles)

    def resolve(self, profile: str) -> PostgresConfig:
        conn = self.profiles.get(profile)
        if conn is None:
            allowed = self.targets()
            msg = f"pg: target {profile!r} is not in the whitelist (allowed={allowed})"
            raise ValueError(msg)
        return conn


class SqlQueryError(RuntimeError):
    """Ошибка выполнения SQL."""


@dataclass(frozen=True)
class SqlResult:
    """Результат SqlExecutor.execute: JSON-safe строки-словари + флаг усечения."""

    rows: list[dict[str, Any]]
    truncated: bool


class SqlExecutor:
    """Исполняет SQL в песочнице; каждый вызов — отдельный процесс и соединение."""

    def __init__(self, *, cfg: SqlExecutorConfig, caller: PgCaller) -> None:
        self._cfg = cfg
        self._caller = caller
        logger.info(
            "SqlExecutor opened: targets=%s max_rows=%d max_bytes=%d",
            cfg.targets(),
            cfg.max_rows,
            cfg.max_bytes,
        )

    @property
    def max_rows_cap(self) -> int:
        return self._cfg.max_rows

    @property
    def max_bytes(self) -> int:
        return self._cfg.max_bytes

    def allowed_targets(self) -> list[str]:
        return self._cfg.targets()

    def connection_of(self, connection_name: str) -> PostgresConfig:
        """Профиль цели с read-only сессией: payload подключается по нему сам."""
        return self._cfg.resolve(connection_name).read_only()

    def execute_copy(self, query: str, *, connection_name: str) -> str:
        try:
            answer = self._caller.copy(
                connection=self.connection_of(connection_name),
                sql=query,
                max_bytes=self._cfg.max_bytes,
            )
        except LauncherError as e:
            raise SqlQueryError(
                f"SQL copy failed (connection_name={connection_name!r}): {e}",
            ) from e
        return answer.text

    def execute(
        self,
        query: str,
        *,
        connection_name: str,
        row_limit: int,
        params: Sequence[Any] | None = None,
    ) -> SqlResult:
        effective_limit = min(max(row_limit, 1), self._cfg.max_rows)
        try:
            answer = self._caller.query(
                connection=self.connection_of(connection_name),
                sql=query,
                params=params or (),
                row_limit=effective_limit,
            )
        except LauncherError as e:
            raise SqlQueryError(
                f"SQL execute failed (connection_name={connection_name!r}): {e}",
            ) from e
        rows: list[dict[str, Any]] = []
        for row in answer.rows:
            rows.append(dict(row))
        return SqlResult(rows=rows, truncated=answer.truncated)
