"""Вызов postgres-payload'а: соединение и запрос идут внутри песочницы."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar

from boba.tool.pg.protocol import (
    PgCopyAnswer,
    PgCopyRequest,
    PgQueryAnswer,
    PgQueryRequest,
)
from boba.toolkit.sandbox import SandboxCaller, SandboxToolConfig

__all__ = ["PgCaller"]


class PgCaller:
    """Один вызов payload'а на запрос; пул не переживает вызов по построению."""

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.pg.payload")

    def __init__(
        self,
        tool: str,
        sandbox: SandboxToolConfig,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> None:
        self._caller = SandboxCaller(tool, sandbox.effective(), path_vars)

    def query(
        self,
        *,
        connection: Mapping[str, Any],
        sql: str,
        params: Sequence[Any],
        row_limit: int,
    ) -> PgQueryAnswer:
        request = PgQueryRequest(
            op=PgQueryRequest.OP,
            connection=dict(connection),
            sql=sql,
            params=tuple(params),
            row_limit=row_limit,
        )
        return self._caller.call_json(self.ENTRY, request, PgQueryAnswer)

    def copy(
        self,
        *,
        connection: Mapping[str, Any],
        sql: str,
        max_bytes: int,
    ) -> PgCopyAnswer:
        request = PgCopyRequest(
            op=PgCopyRequest.OP,
            connection=dict(connection),
            sql=sql,
            max_bytes=max_bytes,
        )
        return self._caller.call_json(self.ENTRY, request, PgCopyAnswer)
