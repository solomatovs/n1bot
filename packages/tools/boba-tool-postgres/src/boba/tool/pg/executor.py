"""
`SqlExecutorConfig` + `SqlExecutor`
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator
from pydantic_core import to_jsonable_python

from boba.db.postgres import PostgresConnection, PostgresPool
from boba.settings.source import TomlEnvConfigSource
from boba.tool.pg.copy_buffer import (
    BufferCapacityError,
    CopyBuffer,
    RowLimitExceededError,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SqlExecutor",
    "SqlExecutorConfig",
    "SqlQueryError",
    "SqlResult",
]


RESULT_BYTES_HARDLIMIT = 1_000_000


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
    max_bytes: int = Field(
        default=RESULT_BYTES_HARDLIMIT,
        ge=1,
        description=(
            "Hardlimit на суммарный размер CSV-результата COPY (байт). "
            f"Default {RESULT_BYTES_HARDLIMIT}. Превышение → ошибка LLM "
            "«добавьте LIMIT»."
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
    """Результат SqlExecutor.execute: JSON-safe строки-словари + флаг усечения.

    `rows` — это уже `list[dict]` (dict_row курсора), значения приведены к
    JSON-safe через `pydantic_core.to_jsonable_python` (Decimal/UUID/datetime
    → строки, jsonb/array остаются вложенной структурой).
    """

    rows: list[dict[str, Any]]
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
            "SqlExecutor opened: targets=%s max_rows=%d max_bytes=%d",
            sorted(cfg.databases),
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
        return sorted(self._cfg.databases)

    def execute_copy(self, query: str, *, target: str) -> CopyBuffer:
        """
        Выполнить `COPY (<query>) TO STDOUT (FORMAT TEXT, HEADER)`
        """
        conn = self._cfg.resolve(target)
        pool = PostgresPool.get(
            conn.to_pool_config(session_options=self._cfg.session_options(conn)),
        )

        stmt = f"COPY ({query}) TO STDOUT WITH (FORMAT TEXT, HEADER)"

        buf = CopyBuffer(
            max_capacity=self._cfg.max_bytes,
            limit_rows=self._cfg.max_rows,
        )
        try:
            with pool.cursor() as cur, cur.copy(stmt) as cp:  # type: ignore[arg-type]
                for block in cp:
                    buf.write(block)
        except (BufferCapacityError, RowLimitExceededError):
            # гард-сигналы — наружу, трактует вызывающий
            raise
        except Exception as e:
            raise SqlQueryError(
                f"SQL copy failed (target={target!r}): {type(e).__name__}: {e}",
            ) from e

        return buf

    def execute(
        self,
        query: str,
        *,
        target: str,
        row_limit: int,
        params: Sequence[Any] | None = None,
    ) -> SqlResult:
        """Выполнить SQL на профиле target; вернуть JSON-safe dict-строки."""
        conn = self._cfg.resolve(target)
        pool = PostgresPool.get(
            conn.to_pool_config(session_options=self._cfg.session_options(conn)),
        )

        effective_limit = min(max(row_limit, 1), self._cfg.max_rows)
        fetch_limit = effective_limit + 1

        try:
            with pool.dict_cursor() as cur:
                # params=None (а не пустой кортеж) — это сигнал psycopg3
                # НЕ парсить query как placeholder-шаблон. Иначе `%` в
                # тексте запроса от LLM (LIKE '%h%', to_char и т.п.) ловит
                # "only '%s','%b','%t' are allowed as placeholders".
                cur.execute(query, params)  # type: ignore[arg-type]
                fetched = cur.fetchmany(fetch_limit)
        except Exception as e:
            raise SqlQueryError(
                f"SQL execute failed (target={target!r}): {type(e).__name__}: {e}",
            ) from e

        truncated = len(fetched) > effective_limit
        # dict_row → list[dict]; значения → JSON-safe (Decimal/UUID/datetime/…)
        rows = to_jsonable_python(fetched[:effective_limit])
        return SqlResult(rows=rows, truncated=truncated)
