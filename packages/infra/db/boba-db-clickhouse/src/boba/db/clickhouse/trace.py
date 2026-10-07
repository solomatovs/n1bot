"""Что сервер ClickHouse сообщает о выполнении помимо данных: сводка
X-ClickHouse-Summary (прочитано, записано, результат, время), id запроса,
имя сервера, часовой пояс и формат ответа. У потокового ответа сводка
приходит в заголовках до конца тела, поэтому счётчики чтения в ней —
на момент начала ответа. Насосы отдают это в чат вместе с итогом.

Ошибок наружу нет: всё берётся из заголовков ответа или сводки драйвера.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from boba.toolkit.result import Fact, SqlStatement

__all__ = [
    "ChCommandReport",
    "ChHeader",
    "ChQueryTrace",
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
    MEMORY_USAGE = "memory_usage"


@dataclass(frozen=True)
class ChCommandReport:
    """Итог команды насоса для чата: сводка насоса, контракт колонок (columns,
    у источника), счётчики сервера и координаты ответа. Сам выполненный
    стейтмент показывает журнал команд вызова."""

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
    columns: Sequence[Mapping[str, str]] = field(default_factory=tuple)

    def sql_statement(self) -> SqlStatement:
        rows: list[Mapping[str, str]] | None = None
        if self.columns:
            rows = list(self.columns)

        return SqlStatement(
            status=self.summary,
            rows=rows,
            facts=list(self._facts()),
        )

    def _facts(self) -> Iterator[Fact]:
        yield Fact(key="read", value=f"{self.read_rows} rows, {self.read_bytes} bytes")
        yield Fact(
            key="written",
            value=f"{self.written_rows} rows, {self.written_bytes} bytes",
        )
        yield Fact(
            key="result", value=f"{self.result_rows} rows, {self.result_bytes} bytes"
        )
        yield Fact(key="elapsed", value=f"{self.elapsed_ns // 1_000_000} ms")
        yield Fact(
            key="server",
            value=(
                f"{self.server}, query id {self.query_id}, "
                f"timezone {self.timezone}, format {self.fmt}"
            ),
        )


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
    def server(self) -> str:
        """Заголовок X-ClickHouse-Server-Display-Name ответа."""
        return self._server

    @property
    def written_rows(self) -> int:
        return self._count(ChSummaryKey.WRITTEN_ROWS)

    @property
    def read_rows(self) -> int:
        return self._count(ChSummaryKey.READ_ROWS)

    @property
    def memory_usage(self) -> int | None:
        """Пик памяти запроса на сервере, байт; None — сервер его в сводке
        не отдаёт (до 25-й версии)."""
        value = self._summary.get(ChSummaryKey.MEMORY_USAGE.value)
        if value is None:
            return None

        return int(value)

    def report(
        self,
        summary: str,
        statement: str,
        columns: Sequence[Mapping[str, str]] = (),
    ) -> ChCommandReport:
        return ChCommandReport(
            summary=summary,
            statement=statement,
            columns=tuple(columns),
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
