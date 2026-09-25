"""Общее у матриц перекачки: описание колонки под приёмник (тип приёмника,
выражение выгрузки, опорные выражения), сборка select и сверка выборок
двух сторон поколоночно."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from boba.pump_stand.compare import Report, Values

__all__ = [
    "Target",
    "column",
    "compared",
    "copy_into",
    "exported",
    "first",
    "insert_into",
]


@dataclass(frozen=True)
class Target:
    """Как колонка едет в приёмник: тип колонки приёмника, выражение выгрузки
    источника под него (пусто — имя колонки), опорные выражения приёмника и
    источника (пусто — имя колонки), допуск для float."""

    type: str
    out: str = ""
    ref: str = ""
    src_ref: str = ""
    approx: float = 0.0


def first(value: str, default: str) -> str:
    if value:
        return value

    return default


def column(rows: Sequence[Sequence[Any]], position: int) -> list[Any]:
    return [row[position] for row in rows]


def exported(names: Sequence[str], targets: Sequence[Target], quoted: bool) -> str:
    """Список select источника: выражение под приёмник с алиасом имени; quoted —
    алиас в двойных кавычках (имена строчными у Oracle)."""
    parts: list[str] = []
    for name, target in zip(names, targets, strict=True):
        alias = name
        if quoted:
            alias = f'"{name}"'

        parts.append(f"{first(target.out, name)} as {alias}")

    return ", ".join(parts)


def copy_into(table: str, names: Sequence[str]) -> str:
    """Стейтмент pg_arrow_in: COPY таблицы по колонкам в порядке полей потока."""
    return f"copy {table} ({', '.join(names)}) from stdin (format csv)"


def insert_into(table: str, names: Sequence[str]) -> str:
    """Стейтмент ora_arrow_in и ora_csv_in: INSERT с bind'ами :1..:n по колонкам."""
    marks = ", ".join(f":{position}" for position in range(1, len(names) + 1))

    return f"insert into {table} ({', '.join(names)}) values ({marks})"  # noqa: S608


def compared(
    names: Sequence[str],
    kinds: Sequence[Values],
    tolerances: Sequence[float],
    expected: Sequence[Sequence[Any]],
    landed: Sequence[Sequence[Any]],
) -> Report:
    report = Report()
    ids = column(expected, 0)
    for position, name in enumerate(names):
        report.compare(
            name,
            kinds[position],
            tolerances[position],
            ids,
            column(expected, position),
            column(landed, position),
        )

    return report
