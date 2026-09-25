"""Что сервер postgres сообщает помимо данных: notices и уведомления notify
за время сессии, статус команды, счётчики сервера. Насосы отдают это в чат
вместе с итогом, чтобы вызывающий видел не только число строк, но и то,
что сервер хотел сказать.

Ошибок наружу нет: сбор идёт обработчиками psycopg на соединении.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import Notify
from psycopg.errors import Diagnostic

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

    def render(self) -> str:
        lines = [f"- {self.severity} {self.sqlstate}: {self.message}"]
        if self.detail:
            lines.append(f"  detail: {self.detail}")

        if self.hint:
            lines.append(f"  hint: {self.hint}")

        return "\n".join(lines)


@dataclass(frozen=True)
class PgNotify:
    """Уведомление NOTIFY, пришедшее в сессию во время команды."""

    channel: str
    payload: str
    pid: int

    def render(self) -> str:
        return f"- {self.channel} from pid {self.pid}: {self.payload}"


@dataclass(frozen=True)
class PgCommandReport:
    """Итог команды насоса для чата: первая строка — сводка насоса, дальше
    статус сервера (у COPY ... TO STDOUT psycopg его не сохраняет — тогда
    строки нет), выполненный стейтмент, сессия и всё, что сервер сообщил."""

    summary: str
    status: str
    statement: str
    backend_pid: int
    server_version: int
    notices: Sequence[PgNotice] = field(default_factory=tuple)
    notifies: Sequence[PgNotify] = field(default_factory=tuple)

    def render(self) -> str:
        lines = [self.summary]
        if self.status:
            lines.append(f"status: {self.status}")

        lines += [
            f"statement: {self.statement}",
            f"server: backend pid {self.backend_pid}, version {self.server_version}",
        ]
        if self.notices:
            lines.append("notices:")
            lines.extend(notice.render() for notice in self.notices)

        if self.notifies:
            lines.append("notifications:")
            lines.extend(notify.render() for notify in self.notifies)

        return "\n".join(lines)


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
        self, summary: str, statement: str, cursor: psycopg.AsyncCursor[Any]
    ) -> PgCommandReport:
        status = cursor.statusmessage
        if status is None:
            status = ""

        return PgCommandReport(
            summary=summary,
            status=status,
            statement=statement,
            backend_pid=self._conn.info.backend_pid,
            server_version=self._conn.info.server_version,
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
