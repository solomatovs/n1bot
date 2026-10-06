"""Перехват ошибок для журнала, чата и истории LLM: любое исключение — результат.

Живёт в ядре: одним видом обязаны говорить и обёртки инструментов, и
песочница, и приложение. Исключение становится наследником FailureResult
(boba.toolkit.result) через упаковщик FailurePacker: ReportedError отдаёт
свой вид сам, любое другое упаковывается единообразно — тип, текст, место,
цепочка причин и трасса.

Ошибки: не выпускает.
"""

from __future__ import annotations

import traceback
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from enum import StrEnum
from pathlib import PurePath
from types import TracebackType
from typing import ClassVar

from pydantic import ValidationError
from pydantic_core import ErrorDetails

from boba.toolkit.result import (
    ErrorResult,
    ExceptionResult,
    FailureCause,
    FailureResult,
)

__all__ = [
    "FailurePacker",
    "InvokeErrorKind",
    "ReportedError",
    "ToolRefusalError",
    "ToolUnavailableError",
    "ValidationText",
]


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


class ReportedError(ABC):
    """Исключение со своим видом ошибки: упаковщик берёт его результат как есть.

    База для исключений, которые показывают себя лучше общей упаковки:
    отказ с текстом для человека, конверт ошибки из процесса инструмента.
    Наследуется рядом с Exception: `class X(Exception, ReportedError)`.
    """

    @abstractmethod
    def failure(self) -> FailureResult:
        """Ошибка в виде результата семейства."""


class ToolRefusalError(Exception, ReportedError):
    """Отказ выполнения: текст готов для пользователя и LLM, причина не нужна.

    Отказ — не сбой: инструмент не начал работу, потому что состояние сессии
    или конфигурации этого не позволяет. Показывать к такому тексту цепочку
    технических причин незачем — она только мешает и человеку, и модели.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind

    def failure(self) -> FailureResult:
        return ErrorResult(message=str(self), error_kind=self.kind)


class FailurePacker:
    """Единая точка превращения исключения в результат-ошибку.

    Зовут её все границы, где исключение перестаёт лететь и становится
    показом: процесс инструмента (ToolMain), обёртка вызова на хосте
    (ToolErrorGuard), группа каналов, сбой хода в чате (FailureReport).
    ReportedError отдаёт свой вид; остальное упаковывается в ExceptionResult.
    Текст звена ValidationError идёт без разобранных данных — в них ездят
    секреты.
    """

    MAX_LINKS: ClassVar[int] = 5
    """Потолок длины цепочки: глубже идут повторы обёрток."""

    PATH_ROOTS: ClassVar[tuple[str, ...]] = ("site-packages", "src")
    """Каталоги, после которых путь файла в месте возникновения уже читаем."""

    OWN_CODE: ClassVar[str] = "boba/"
    """Префикс короткого пути нашего кода: место ищется в нём, а не в драйвере."""

    CAUSE_SEPARATOR: ClassVar[str] = (
        "\n\nThe above exception was the direct cause of the following exception:\n\n"
    )

    def __init__(self) -> None:
        self._validation = ValidationText()

    def pack(self, error: BaseException) -> FailureResult:
        if isinstance(error, ReportedError):
            return error.failure()

        links = list(self._chain(error))
        causes = list(self._causes(links[1:]))

        return ExceptionResult(
            error_kind=type(error).__name__,
            message=self._text(error),
            causes=causes,
            raised_at=self._raised_at(error.__traceback__),
            traceback=self._traceback(links),
        )

    def _chain(self, error: BaseException) -> Iterator[BaseException]:
        """Исключение и его причины по `from` и неявному контексту."""
        seen: set[int] = set()
        current: BaseException | None = error

        while current is not None and len(seen) < self.MAX_LINKS:
            if id(current) in seen:
                return

            seen.add(id(current))
            yield current

            if current.__cause__ is not None:
                current = current.__cause__
                continue

            if not current.__suppress_context__:
                current = current.__context__
                continue

            return

    def _causes(self, links: Sequence[BaseException]) -> Iterator[FailureCause]:
        for link in links:
            yield FailureCause(error_type=type(link).__name__, message=self._text(link))

    def _text(self, error: BaseException) -> str:
        if isinstance(error, ValidationError):
            return self._validation.of(error)

        return str(error).strip()

    def _raised_at(self, tb: TracebackType | None) -> str:
        """Самый глубокий кадр нашего кода; без него — самый глубокий вообще.

        Кадр внутри драйвера или pydantic не говорит, где искать: важен
        вызов из нашего кода, который туда привёл.
        """
        frames = traceback.extract_tb(tb)
        if not frames:
            return ""

        chosen = frames[-1]
        for frame in reversed(frames):
            if self._short_path(frame.filename).startswith(self.OWN_CODE):
                chosen = frame
                break

        return f"{self._short_path(chosen.filename)}:{chosen.lineno} in {chosen.name}"

    def _short_path(self, filename: str) -> str:
        parts = PurePath(filename).parts
        for root in self.PATH_ROOTS:
            if root not in parts:
                continue

            index = len(parts) - 1 - parts[::-1].index(root)
            return "/".join(parts[index + 1 :])

        return filename

    def _traceback(self, links: Sequence[BaseException]) -> str:
        """Трасса в порядке Python: от исходной причины к верхней обёртке.

        Строку исключения пишет упаковщик, а не traceback.format_exception:
        тот печатает ValidationError вместе с входными данными.
        """
        sections: list[str] = []
        for link in reversed(links):
            sections.append(self._section(link))

        return self.CAUSE_SEPARATOR.join(sections)

    def _section(self, link: BaseException) -> str:
        frames = traceback.format_list(traceback.extract_tb(link.__traceback__))
        head = "Traceback (most recent call last):\n"

        line = type(link).__name__
        if text := self._text(link):
            line = f"{line}: {text}"

        return head + "".join(frames) + line


class ToolUnavailableError(Exception):
    """Инструмент не виден субъекту вне чата: не собран или запрещён."""


class InvokeErrorKind(StrEnum):
    """Коды error_kind результатов, которые ставит исполнитель вместо инструмента."""

    TOOL_ERROR = "tool_error"
    """Итог вызова со статусом ошибки без результата: только текст отказа."""
