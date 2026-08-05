"""ChExecutorConfig + ChExecutor."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.db.clickhouse import ClickHouseConfig
from boba.tool.ch.caller import ChCaller
from boba.toolkit.launcher import LauncherError, RowCollector

logger = logging.getLogger(__name__)

__all__ = [
    "ChExecutor",
    "ChExecutorConfig",
    "ChQueryError",
    "ChResult",
]


class ChExecutorConfig(BaseModel):
    """Конфиг для ChExecutor."""

    model_config = ConfigDict(extra="ignore")

    profiles: dict[str, ClickHouseConfig] = Field(
        default_factory=dict,
        description=(
            "dict[target, clickhouse-профиль ссылкой]: "
            '`[tool.ch.profiles] main = "${clickhouse.main}"`. '
            "Ключ — значение tool-arg `connection_name` (LLM выбирает БД по нему)."
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
            "Hardlimit на суммарный размер собранных строк (символов). "
            f"Default {1_000_000}. Превышение -> ошибка LLM «добавьте LIMIT»."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.profiles:
            msg = (
                "tool.ch: no profiles configured. Add [clickhouse.<name>] and "
                'reference it: [tool.ch.profiles] <name> = "${clickhouse.<name>}".'
            )
            raise ValueError(msg)
        return self

    def targets(self) -> list[str]:
        return sorted(self.profiles)

    def resolve(self, profile: str) -> ClickHouseConfig:
        conn = self.profiles.get(profile)
        if conn is None:
            allowed = self.targets()
            msg = f"ch: target {profile!r} is not in the whitelist (allowed={allowed})"
            raise ValueError(msg)
        return conn


class ChQueryError(RuntimeError):
    """Ошибка выполнения SQL."""


@dataclass(frozen=True)
class ChResult:
    """Результат ChExecutor.execute: JSON-safe строки-словари + флаг усечения."""

    rows: list[dict[str, Any]]
    truncated: bool


class ChExecutor:
    """Исполняет SQL в песочнице; каждый вызов — отдельный процесс и клиент."""

    def __init__(self, *, cfg: ChExecutorConfig, caller: ChCaller) -> None:
        self._cfg = cfg
        self._caller = caller
        logger.info(
            "ChExecutor opened: targets=%s max_rows=%d max_bytes=%d",
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

    def connection_of(self, connection_name: str) -> ClickHouseConfig:
        """Профиль цели с readonly-сессией: payload подключается по нему сам."""
        return self._cfg.resolve(connection_name).read_only()

    def execute(
        self,
        query: str,
        *,
        connection_name: str,
        row_limit: int,
        params: Mapping[str, Any] | None = None,
    ) -> ChResult:
        effective_limit = min(max(row_limit, 1), self._cfg.max_rows)
        collector = RowCollector(
            max_chars=self._cfg.max_bytes,
            limit_rows=effective_limit,
        )
        try:
            trailer = self._caller.query(
                connection=self.connection_of(connection_name),
                sql=query,
                params=params or {},
                row_limit=effective_limit,
                sink=collector,
            )
        except LauncherError as e:
            raise ChQueryError(
                f"SQL execute failed (connection_name={connection_name!r}): {e}",
            ) from e
        return ChResult(rows=collector.rows(), truncated=trailer.truncated)
