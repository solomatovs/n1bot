"""Единая формулировка сбоя для журнала, чата и истории LLM.

Живёт в ядре: одним текстом обязаны говорить и обёртки инструментов, и
песочница, и приложение — иначе одна и та же авария выглядит в трёх местах
по-разному, и поиск причины начинается со сверки формулировок.

Ошибки: не выпускает.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import ClassVar

from pydantic import ValidationError
from pydantic_core import ErrorDetails

__all__ = ["FailureText", "ToolRefusalError", "ValidationText"]


class ValidationText:
    """Текст ошибки валидации без разобранных данных: в них ездят секреты.

    str(ValidationError) печатает input_value целиком — для профиля
    соединения это словарь с логином и паролем, а он уходит и в чат, и в
    журнал. Наружу идут только адрес поля, формулировка и код ошибки.
    """

    SEPARATOR: ClassVar[str] = "; "
    MAX_ERRORS: ClassVar[int] = 5
    ROOT: ClassVar[str] = "<root>"
    PATH_SEPARATOR: ClassVar[str] = "."

    @classmethod
    def of(cls, error: ValidationError) -> str:
        """Все нарушения одной строкой; хвост длинного списка сворачивается."""
        reports = error.errors(
            include_url=False,
            include_input=False,
            include_context=False,
        )

        lines: list[str] = []
        for report in reports[: cls.MAX_ERRORS]:
            lines.append(cls._one(report))

        hidden = len(reports) - len(lines)
        if hidden > 0:
            lines.append(f"and {hidden} more")

        return cls.SEPARATOR.join(lines)

    @classmethod
    def _one(cls, report: ErrorDetails) -> str:
        location = cls._location(report["loc"])

        return f"{location}: {report['msg']} [{report['type']}]"

    @classmethod
    def _location(cls, loc: Sequence[int | str]) -> str:
        if not loc:
            return cls.ROOT

        parts: list[str] = []
        for item in loc:
            parts.append(str(item))

        return cls.PATH_SEPARATOR.join(parts)


class ToolRefusalError(Exception):
    """Отказ выполнения: текст готов для пользователя и LLM, причина не нужна.

    Отказ — не сбой: инструмент не начал работу, потому что состояние сессии
    или конфигурации этого не позволяет. Показывать к такому тексту цепочку
    технических причин незачем — она только мешает и человеку, и модели.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class FailureText:
    """Единая формулировка сбоя: тип, сообщение и цепочка причин.

    Лог, чат и история обязаны говорить об одном и том же: пользователь
    пересказывает текст в задачу, LLM правит по нему свой следующий шаг, а
    инженер ищет по нему же в журнале. Трейсбек остаётся только в логе —
    в чат едут строки причин, они и объясняют сбой.
    """

    SEPARATOR: ClassVar[str] = " <- "
    """Разделитель звеньев: слева — что упало, справа — из-за чего."""

    MAX_LINKS: ClassVar[int] = 5
    """Потолок длины цепочки: глубже идут повторы обёрток."""

    @classmethod
    def of(cls, error: BaseException) -> str:
        """Строка сбоя целиком: `Type: message <- Cause: message`.

        У отказа цепочки нет: его текст и есть объяснение.
        """
        if isinstance(error, ToolRefusalError):
            return str(error)

        links: list[str] = []
        for link in cls._chain(error):
            links.append(cls._one(link))

        return cls.SEPARATOR.join(links)

    @classmethod
    def _chain(cls, error: BaseException) -> Iterator[BaseException]:
        """Ошибка и её причины; повторы и пустые звенья пропускаются."""
        seen: set[int] = set()
        current: BaseException | None = error

        while current is not None and len(seen) < cls.MAX_LINKS:
            if id(current) in seen:
                return

            seen.add(id(current))
            yield current

            if current.__cause__ is not None:
                current = current.__cause__
                continue

            # implicit-цепочка (raise внутри except) тоже объясняет причину
            if not current.__suppress_context__:
                current = current.__context__
                continue

            return

    @staticmethod
    def _one(error: BaseException) -> str:
        """Звено цепочки: тип плюс текст; у части библиотечных он пуст."""
        if isinstance(error, ValidationError):
            return f"{type(error).__name__}: {ValidationText.of(error)}"

        text = str(error).strip()
        if not text:
            return type(error).__name__

        return f"{type(error).__name__}: {text}"
