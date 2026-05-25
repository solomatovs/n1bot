"""
`SqlExecutorConfig` + `SqlExecutor`
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator

from boba.db.postgres import PostgresConnection, PostgresPool
from boba.settings.source import TomlEnvConfigSource

logger = logging.getLogger(__name__)

__all__ = ["SqlExecutor", "SqlExecutorConfig", "SqlQueryError", "SqlResult"]


CELL_CHARS_HARDLIMIT = 2000


class SqlExecutorConfig(BaseModel):
    """Конфиг для SqlExecutor."""

    profiles: list[str] = Field(
        default_factory=list,
        description=(
            "Whitelist имён postgres-профилей (`[postgres.<name>]`), "
            "доступных LLM. Имя из списка передаётся в tool-arg `target`. "
            "Все connection-поля (host/auth/application_name/timeout/…) "
            "живут в самой секции `[postgres.<name>]`."
        ),
    )
    databases: dict[str, PostgresConnection] = Field(
        default_factory=dict,
        description=(
            "Computed: profiles → dict; заполняется `_resolve_profiles`. "
            "Оператор это поле не задаёт."
        ),
    )
    max_rows: int = Field(
        default=100,
        ge=1,
        description="Ограничение по кол-ву строк.",
    )
    max_cell_chars: int = Field(
        default=CELL_CHARS_HARDLIMIT,
        ge=1,
        description=(
            f"Hardlimit на длину одного cell-значения. Default {CELL_CHARS_HARDLIMIT}."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _resolve_profiles(cls, values: Any) -> Any:
        """`profiles: list[str]` → `databases: dict[name, PostgresConnection]`.

        Для каждого имени читает `[postgres.<name>]` через
        `TomlEnvConfigSource.for_path`. Отсутствующая или пустая
        секция → `ValueError`. Поле `databases` в input игнорируется
        и заменяется на computed; cмешивать недопустимо.
        """
        if not isinstance(values, Mapping):
            return values
        profiles = values.get("profiles")
        if not profiles:
            return values

        toml_source = TomlEnvConfigSource()
        resolved: dict[str, Any] = {}
        for name in profiles:
            name_s = str(name)
            shared = toml_source.for_path(("postgres", name_s))
            if not shared:
                msg = (
                    f"tool.pg.profiles: профиль {name_s!r} — "
                    f"секция [postgres.{name_s}] не найдена или пуста"
                )
                raise ValueError(msg)
            resolved[name_s] = dict(shared)

        new_values = dict(values)
        new_values["databases"] = resolved
        return new_values

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.databases:
            msg = "tool.pg.profiles: список профилей пуст"
            raise ValueError(msg)
        return self

    def resolve(self, target: str) -> PostgresConnection:
        """Вернуть PostgresConnection для target; ValueError если не в whitelist."""
        conn = self.databases.get(target)
        if conn is None:
            allowed = sorted(self.databases)
            msg = f"pg: target {target!r} не в whitelist (allowed={allowed})"
            raise ValueError(msg)
        return conn

    @staticmethod
    def session_options(conn: PostgresConnection) -> dict[str, str]:
        """Session-level GUC, зашиваемые в options DSN."""
        return {
            "default_transaction_read_only": "on",
            "statement_timeout": str(conn.statement_timeout_ms),
        }


class SqlQueryError(RuntimeError):
    """Ошибка выполнения SQL."""


@dataclass(frozen=True)
class SqlResult:
    """Результат SqlExecutor.execute."""

    columns: list[str]
    rows: list[tuple[Any, ...]]
    row_count: int
    limit_applied: int
    truncated: bool


class SqlExecutor:
    """Pool кешируется в PostgresPool.get по DSN, поэтому повторные вызовы дешёвые."""

    def __init__(
        self,
        *,
        cfg: SqlExecutorConfig,
    ) -> None:
        self._cfg = cfg
        logger.info(
            "SqlExecutor opened: targets=%s max_rows=%d max_cell_chars=%d",
            sorted(cfg.databases), cfg.max_rows, cfg.max_cell_chars,
        )

    @property
    def max_cell_chars(self) -> int:
        return self._cfg.max_cell_chars

    @property
    def max_rows_cap(self) -> int:
        return self._cfg.max_rows

    def allowed_targets(self) -> list[str]:
        return sorted(self._cfg.databases)

    def execute(
        self,
        query: str,
        *,
        target: str,
        row_limit: int,
        params: Sequence[Any] | None = None,
    ) -> SqlResult:
        """Выполнить SQL на профиле target; вернуть rows + meta."""
        conn = self._cfg.resolve(target)
        pool = PostgresPool.get(
            conn.to_pool_config(session_options=self._cfg.session_options(conn)),
        )

        effective_limit = min(max(row_limit, 1), self._cfg.max_rows)
        fetch_limit = effective_limit + 1

        try:
            with pool.cursor() as cur:
                cur.execute(query, params or ())  # type: ignore[arg-type]
                fetched = cur.fetchmany(fetch_limit)
                columns = [d.name for d in (cur.description or [])]
        except Exception as e:
            raise SqlQueryError(
                f"SQL execute failed (target={target!r}): {type(e).__name__}: {e}",
            ) from e

        truncated = len(fetched) > effective_limit
        rows = [tuple(row) for row in fetched[:effective_limit]]
        return SqlResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            limit_applied=effective_limit,
            truncated=truncated,
        )
