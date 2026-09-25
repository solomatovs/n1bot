"""Что сервер ClickHouse сообщает о выполнении помимо данных: сводка
X-ClickHouse-Summary (прочитано, записано, результат, время), id запроса,
имя сервера, часовой пояс и формат ответа. У потокового ответа сводка
приходит в заголовках до конца тела, поэтому счётчики чтения в ней —
на момент начала ответа. Насосы отдают это в чат вместе с итогом.

Ошибок наружу нет: всё берётся из заголовков ответа или сводки драйвера.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum

__all__ = [
    "ChCommandReport",
    "ChHeader",
    "ChQueryTrace",
    "ChScriptStep",
    "ChSummaryKey",
]


class ChHeader(StrEnum):
    """Заголовки ответа HTTP ClickHouse, из которых собирается сводка."""

    SUMMARY = "X-ClickHouse-Summary"
    QUERY_ID = "X-ClickHouse-Query-Id"
    SERVER = "X-ClickHouse-Server-Display-Name"
    TIMEZONE = "X-ClickHouse-Timezone"
    FORMAT = "X-ClickHouse-Format"


class ChSummaryKey(StrEnum):
    """Ключи JSON-сводки X-ClickHouse-Summary."""

    READ_ROWS = "read_rows"
    READ_BYTES = "read_bytes"
    WRITTEN_ROWS = "written_rows"
    WRITTEN_BYTES = "written_bytes"
    RESULT_ROWS = "result_rows"
    RESULT_BYTES = "result_bytes"
    ELAPSED_NS = "elapsed_ns"


@dataclass(frozen=True)
class ChScriptStep:
    """Итог одного стейтмента скрипта before/after насоса: текст и что
    ответил сервер — счётчики сводки у команды или значение у выборки."""

    statement: str
    outcome: str

    def render(self) -> str:
        return f"- {self.outcome}: {self.statement}"


@dataclass(frozen=True)
class ChCommandReport:
    """Итог команды насоса для чата: первая строка — сводка насоса, дальше
    счётчики сервера, стейтмент, шаги скриптов before и after той же сессии
    и координаты ответа."""

    summary: str
    statement: str
    query_id: str
    server: str
    timezone: str
    fmt: str
    read_rows: int
    read_bytes: int
    written_rows: int
    written_bytes: int
    result_rows: int
    result_bytes: int
    elapsed_ns: int
    before: Sequence[ChScriptStep] = field(default_factory=tuple)
    after: Sequence[ChScriptStep] = field(default_factory=tuple)

    def scripted(
        self, before: Sequence[ChScriptStep], after: Sequence[ChScriptStep]
    ) -> ChCommandReport:
        return replace(self, before=tuple(before), after=tuple(after))

    def render(self) -> str:
        elapsed_ms = self.elapsed_ns // 1_000_000

        lines = [
            self.summary,
            f"read: {self.read_rows} rows, {self.read_bytes} bytes; "
            f"written: {self.written_rows} rows, {self.written_bytes} bytes; "
            f"result: {self.result_rows} rows, {self.result_bytes} bytes; "
            f"elapsed: {elapsed_ms} ms",
            f"statement: {self.statement}",
        ]
        if self.before:
            lines.append("before:")
            lines.extend(step.render() for step in self.before)

        if self.after:
            lines.append("after:")
            lines.extend(step.render() for step in self.after)

        lines.append(
            f"server: {self.server}, query id {self.query_id}, "
            f"timezone {self.timezone}, format {self.fmt}"
        )

        return "\n".join(lines)


class ChQueryTrace:
    """Сводка одного запроса: словарь X-ClickHouse-Summary (из заголовков
    ответа или из QuerySummary драйвера) и координаты ответа."""

    def __init__(
        self,
        summary: Mapping[str, str],
        query_id: str,
        server: str,
        timezone: str,
        fmt: str,
    ) -> None:
        self._summary = dict(summary)
        self._query_id = query_id
        self._server = server
        self._timezone = timezone
        self._fmt = fmt

    @property
    def written_rows(self) -> int:
        return self._count(ChSummaryKey.WRITTEN_ROWS)

    @property
    def read_rows(self) -> int:
        return self._count(ChSummaryKey.READ_ROWS)

    def report(self, summary: str, statement: str) -> ChCommandReport:
        return ChCommandReport(
            summary=summary,
            statement=statement,
            query_id=self._query_id,
            server=self._server,
            timezone=self._timezone,
            fmt=self._fmt,
            read_rows=self._count(ChSummaryKey.READ_ROWS),
            read_bytes=self._count(ChSummaryKey.READ_BYTES),
            written_rows=self._count(ChSummaryKey.WRITTEN_ROWS),
            written_bytes=self._count(ChSummaryKey.WRITTEN_BYTES),
            result_rows=self._count(ChSummaryKey.RESULT_ROWS),
            result_bytes=self._count(ChSummaryKey.RESULT_BYTES),
            elapsed_ns=self._count(ChSummaryKey.ELAPSED_NS),
        )

    def _count(self, key: ChSummaryKey) -> int:
        value = self._summary.get(key.value)
        if value is None:
            return 0

        return int(value)
