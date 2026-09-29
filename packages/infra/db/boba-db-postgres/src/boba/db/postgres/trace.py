"""Что сервер postgres сообщает помимо данных: notices и уведомления notify
за время сессии, статус команды, счётчики сервера. Насосы отдают это в чат
вместе с итогом, чтобы вызывающий видел не только число строк, но и то,
что сервер хотел сказать.

Ошибок наружу нет: сбор идёт обработчиками psycopg на соединении.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import Notify
from psycopg.errors import Diagnostic

from boba.toolkit.result import Fact, SqlStatement

__all__ = ["PgCommandReport", "PgNotice", "PgNotify", "PgSessionTrace"]


@dataclass(frozen=True)
class PgNotice:
    """Сообщение сервера уровня NOTICE/WARNING и подобных: RAISE в plpgsql,
    предупреждения планировщика, усечения."""

    severity: str
    sqlstate: str
    message: str
    detail: str
    hint: str

    def fact(self) -> Fact:
        lines = [self.message]
        if self.detail:
            lines.append(f"detail: {self.detail}")

        if self.hint:
            lines.append(f"hint: {self.hint}")

        return Fact(key=f"{self.severity} {self.sqlstate}", value="\n".join(lines))


@dataclass(frozen=True)
class PgNotify:
    """Уведомление NOTIFY, пришедшее в сессию во время команды."""

    channel: str
    payload: str
    pid: int

    def fact(self) -> Fact:
        return Fact(
            key=f"notify {self.channel}", value=f"{self.payload} (pid {self.pid})"
        )


@dataclass(frozen=True)
class PgCommandReport:
    """Итог команды насоса для чата командой SqlResult: выполненный стейтмент
    со сводкой насоса, контрактом колонок (columns, у источника), статусом и
    сессией сервера и всем, что сервер сообщил."""

    summary: str
    status: str
    statement: str
    backend_pid: int
    server_version: int
    rows: int = 0
    columns: Sequence[Mapping[str, str]] = field(default_factory=tuple)
    notices: Sequence[PgNotice] = field(default_factory=tuple)
    notifies: Sequence[PgNotify] = field(default_factory=tuple)

    def sql_statement(self) -> SqlStatement:
        rows: list[Mapping[str, str]] | None = None
        if self.columns:
            rows = list(self.columns)

        return SqlStatement(
            text=self.statement,
            status=self.summary,
            rows=rows,
            facts=list(self._facts()),
        )

    def _facts(self) -> Iterator[Fact]:
        if self.status:
            yield Fact(key="status", value=self.status)

        yield Fact(
            key="server",
            value=f"backend pid {self.backend_pid}, version {self.server_version}",
        )

        for notice in self.notices:
            yield notice.fact()

        for notify in self.notifies:
            yield notify.fact()


class PgSessionTrace:
    """Сбор notices и notify соединения обработчиками psycopg с момента
    создания; report собирает итог команды по её курсору."""

    def __init__(self, conn: psycopg.AsyncConnection[Any]) -> None:
        self._conn = conn
        self._notices: list[PgNotice] = []
        self._notifies: list[PgNotify] = []
        conn.add_notice_handler(self._on_notice)
        conn.add_notify_handler(self._on_notify)

    def report(
        self,
        summary: str,
        statement: str,
        cursor: psycopg.AsyncCursor[Any],
        rows: int = 0,
    ) -> PgCommandReport:
        status = cursor.statusmessage
        if status is None:
            status = ""

        return self.report_status(summary, statement, status, rows)

    def report_status(
        self,
        summary: str,
        statement: str,
        status: str,
        rows: int = 0,
        columns: Sequence[Mapping[str, str]] = (),
    ) -> PgCommandReport:
        """Итог команды по строке статуса сервера, когда курсора нет."""
        return PgCommandReport(
            summary=summary,
            status=status,
            statement=statement,
            backend_pid=self._conn.info.backend_pid,
            server_version=self._conn.info.server_version,
            rows=rows,
            columns=tuple(columns),
            notices=tuple(self._notices),
            notifies=tuple(self._notifies),
        )

    def _on_notice(self, diagnostic: Diagnostic) -> None:
        self._notices.append(
            PgNotice(
                severity=self._text(diagnostic.severity),
                sqlstate=self._text(diagnostic.sqlstate),
                message=self._text(diagnostic.message_primary),
                detail=self._text(diagnostic.message_detail),
                hint=self._text(diagnostic.message_hint),
            )
        )

    def _on_notify(self, notify: Notify) -> None:
        self._notifies.append(
            PgNotify(channel=notify.channel, payload=notify.payload, pid=notify.pid)
        )

    @staticmethod
    def _text(value: str | None) -> str:
        if value is None:
            return ""

        return value
