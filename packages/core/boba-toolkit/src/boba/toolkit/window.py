"""Окно выдачи инструмента: аргументы offset/limit для LLM и сборка страницы.

Одно и то же окно листает выборку SQL, ответ HTTP-поиска и список из памяти:
страница копит строки до предела и считает навигацию для note. О видах
результата инструмента модуль не знает: строки и note забирает тот, кто
собирает SqlStatement или TableResult.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "RowLimit",
    "RowOffset",
    "RowPage",
    "RowWindow",
]


RowOffset = Annotated[
    int,
    Field(
        ge=0,
        description=(
            "Сколько строк пропустить: 0 — первая страница. Следующую бери "
            "тем же вызовом со значением next offset из note предыдущей."
        ),
    ),
]
"""LLM-аргумент offset: начало окна выдачи."""

RowLimit = Annotated[
    int,
    Field(ge=1, description="Сколько строк вернуть"),
]
"""LLM-аргумент limit: высота окна выдачи."""


class RowWindow(BaseModel):
    """Окно выдачи, которым правит LLM: что пропустить и сколько отдать.

    Модель листает сама: следующая страница — тот же вызов с offset,
    сдвинутым на limit. Потолка со стороны приложения нет, границы
    выдачи целиком в этих числах.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    offset: int = Field(ge=0)
    limit: int = Field(ge=1)

    def probe(self) -> int:
        """Сколько строк тянуть у источника, который пропуск не делает: окно,
        а сверху разведочная строка.

        Лишняя строка не показывается: по ней видно, что данные не кончились.
        """
        return self.offset + self.limit + 1

    def served_probe(self) -> int:
        """Сколько строк просить у источника, который offset применил сам:
        только окно и разведочная строка."""
        return self.limit + 1


class RowPage:
    """Страница выборки по окну: пропуск, накопление и навигация в note.

    Заполняется построчно через add (потоки драйверов) или разом через take
    (готовые списки); останавливается мягко, потому что предел выдачи
    назначила сама модель и продолжение достаётся следующим вызовом.
    skipped — сколько строк пропустил сам источник (серверный start у
    Confluence, OFFSET в SQL): страница пропуск не повторяет, а навигацию
    считает от offset окна.
    """

    def __init__(self, window: RowWindow, skipped: int) -> None:
        if skipped > window.offset:
            raise ValueError(
                f"row page: source skipped {skipped} rows beyond the window "
                f"offset {window.offset}"
            )

        self._window = window
        self._rows: list[Mapping[str, Any]] = []
        self._skipped = skipped
        self._more = False

    @property
    def more(self) -> bool:
        """Данные за окном остались: следующий вызов их достанет."""
        return self._more

    @property
    def rows(self) -> list[Mapping[str, Any]]:
        """Показанные строки окна."""
        return self._rows

    def add(self, row: Mapping[str, Any]) -> bool:
        """Кладёт строку в окно; False — окно набрано, дальше не добавлять."""
        if self._skipped < self._window.offset:
            self._skipped += 1
            return True

        if len(self._rows) >= self._window.limit:
            self._more = True
            return False

        self._rows.append(row)

        return True

    def take(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Кладёт строки по порядку, пока окно не набрано."""
        for row in rows:
            if not self.add(row):
                return

    def note(self) -> str:
        """Навигация для модели: что показано и с какого offset брать дальше."""
        if not self._rows:
            return f"no rows at offset {self._window.offset}"

        first = self._window.offset + 1
        last = self._window.offset + len(self._rows)
        shown = f"rows {first}-{last}"

        if not self._more:
            return f"{shown}; end of result"

        return f"{shown}; more rows available, next offset={last}"
