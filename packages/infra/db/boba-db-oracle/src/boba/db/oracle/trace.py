"""Что драйвер Oracle сообщает о выполнении помимо данных: число затронутых
или прочитанных строк, предупреждения соединения и стейтмента (PL/SQL с
ошибками компиляции, истекающий пароль), координаты сессии. Насосы отдают
это в чат вместе с итогом.

Ошибок наружу нет: всё берётся из атрибутов соединения и курсора.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from oracledb import AsyncConnection, AsyncCursor

__all__ = ["OraCommandReport", "OraSessionTrace"]


@dataclass(frozen=True)
class OraCommandReport:
    """Итог команды насоса для чата: первая строка — сводка насоса, дальше
    строки, стейтмент, сессия и предупреждения драйвера."""

    summary: str
    statement: str
    rows: int
    session_id: int
    serial_num: int
    instance_name: str
    db_name: str
    service_name: str
    version: str
    warnings: Sequence[str] = field(default_factory=tuple)

    def render(self) -> str:
        lines = [
            self.summary,
            f"rows: {self.rows}",
            f"statement: {self.statement}",
            f"session: sid {self.session_id} serial {self.serial_num}, "
            f"instance {self.instance_name}, db {self.db_name}, "
            f"service {self.service_name}, version {self.version}",
        ]
        if self.warnings:
            lines.append("warnings:")
            lines.extend(f"- {warning}" for warning in self.warnings)

        return "\n".join(lines)


class OraSessionTrace:
    """Сбор итогов с соединения и курсоров: took берёт rowcount и
    предупреждение курсора после команды, took_rows — строки, прочитанные
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

        warning = cursor.warning
        if warning is not None:
            self._warnings.append(str(warning))

    def took_rows(self, rows: int) -> None:
        self._rows += rows

    def report(self, summary: str, statement: str) -> OraCommandReport:
        return OraCommandReport(
            summary=summary,
            statement=statement,
            rows=self._rows,
            session_id=self._conn.session_id,
            serial_num=self._conn.serial_num,
            instance_name=self._conn.instance_name,
            db_name=self._conn.db_name,
            service_name=self._conn.service_name,
            version=self._conn.version,
            warnings=tuple(self._warnings),
        )
