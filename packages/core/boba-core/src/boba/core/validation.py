"""Текст ошибки валидации pydantic без разобранных данных.

Ошибки: не выпускает.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from pydantic import ValidationError
from pydantic_core import ErrorDetails

__all__ = ["ValidationText"]


class ValidationText:
    """Текст ошибки валидации без разобранных данных: в них ездят секреты.

    str(ValidationError) печатает input_value целиком — для профиля
    соединения это словарь с логином и паролем, для аргументов инструмента
    всё, что прислала модель, — а он уходит и в чат, и в журнал. Наружу
    идут только адрес поля, формулировка и код ошибки; хвост длинного
    списка сворачивается.
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
