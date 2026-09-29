"""Что драйвер Oracle сообщает о выполнении помимо данных: число затронутых
или прочитанных строк, предупреждения соединения и стейтмента (PL/SQL с
ошибками компиляции, истекающий пароль), координаты сессии. Насосы отдают
это в чат вместе с итогом.

Ошибок наружу нет: всё берётся из атрибутов соединения и курсора.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from oracledb import AsyncConnection, AsyncCursor

from boba.toolkit.result import Fact, SqlStatement

__all__ = [
    "OraCommandReport",
    "OraSessionTrace",
]


@dataclass(frozen=True)
class OraCommandReport:
    """Итог команды насоса для чата командой SqlResult: выполненный стейтмент
    со сводкой насоса, контрактом колонок (columns, у источника), числом
    строк, сессией и предупреждениями драйвера."""

    summary: str
    statement: str
    rows: int
    session_id: int
    serial_num: int
    instance_name: str
    db_name: str
    service_name: str
    version: str
    columns: Sequence[Mapping[str, str]] = field(default_factory=tuple)
    warnings: Sequence[str] = field(default_factory=tuple)

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
        yield Fact(key="rows", value=str(self.rows))
        yield Fact(
            key="session",
            value=(
                f"sid {self.session_id} serial {self.serial_num}, "
                f"instance {self.instance_name}, db {self.db_name}, "
                f"service {self.service_name}, version {self.version}"
            ),
        )

        for warning in self.warnings:
            yield Fact(key="warning", value=warning)


class OraSessionTrace:
    """Сбор итогов с соединения и курсоров: took берёт rowcount и
    предупреждение курсора после команды, warned — только предупреждение
    (шаги скриптов строк насоса не считают), took_rows — строки, прочитанные
    без курсора (fetch_df_batches); report собирает итог по соединению."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn
        self._rows = 0
        self._warnings: list[str] = []
        warning = conn.warning
        if warning is not None:
            self._warnings.append(str(warning))

    def took(self, cursor: AsyncCursor) -> None:
        if cursor.rowcount > 0:
            self._rows += cursor.rowcount

        self.warned(cursor)

    def warned(self, cursor: AsyncCursor) -> None:
        warning = cursor.warning
        if warning is not None:
            self.warn(str(warning))

    def warn(self, warning: str) -> None:
        """Предупреждение, снятое с курсора вызывающим (RowStream.warning)."""
        self._warnings.append(warning)

    def took_rows(self, rows: int) -> None:
        self._rows += rows

    def report(
        self,
        summary: str,
        statement: str,
        columns: Sequence[Mapping[str, str]] = (),
    ) -> OraCommandReport:
        return OraCommandReport(
            summary=summary,
            statement=statement,
            columns=tuple(columns),
            rows=self._rows,
            session_id=self._conn.session_id,
            serial_num=self._conn.serial_num,
            instance_name=self._conn.instance_name,
            db_name=self._conn.db_name,
            service_name=self._conn.service_name,
            version=self._conn.version,
            warnings=tuple(self._warnings),
        )
