"""Сборщик запросов Oracle: куски текста и имён склеиваются подряд, `:name` в
тексте — bind-параметр python-oracledb в словаре.

Имена квотирует драйвер по своим правилам для идентификаторов и литералов:
OraIdentifier — простое или составное имя (HR.EMPLOYEES, obj$, x@link) идёт как
есть и регистр решает сервер, любое другое (пробел, точка в имени, цифра в
начале) уезжает в двойных кавычках как написано; OraIdentifiers — список имён
через запятую; OraLiterals — список строковых литералов для `in (...)`;
OraBindMarks — позиционные метки `:1, :2, ...` по числу колонок. Драйвер
импортируется в момент рендера: модуль читает и приложение, где драйвера нет.

Ошибки:
QueryBuildError — имя пустое или с кавычкой внутри, список пуст, один параметр
    привязан с двумя значениями, или в bind пришло имя вместо значения.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Self

from boba.toolkit.sql import AbstractQuery, QueryBuilder, QueryBuildError, QueryParams

__all__ = [
    "OraBindMarks",
    "OraIdentifier",
    "OraIdentifiers",
    "OraLiterals",
    "OraPiece",
    "OraQuery",
    "OraQueryBuilder",
]

OraQuery = AbstractQuery[str, QueryParams | None]
"""Собранный запрос: текст с `:name` плюс словарь bind-параметров."""


@dataclass(frozen=True)
class OraIdentifier:
    """Имя объекта Oracle для текста запроса: простое или составное имя как есть,
    иное в двойных кавычках; решает и квотирует драйвер."""

    name: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise QueryBuildError("oracle identifier: expected a non-empty name")

    def render(self) -> str:
        import oracledb  # noqa: PLC0415

        if oracledb.is_qualified_sql_name(self.name):
            return self.name

        try:
            return oracledb.enquote_name(self.name, capitalize=False)
        except oracledb.Error as exc:
            raise QueryBuildError(
                f"oracle identifier {self.name!r} cannot be quoted: {exc}"
            ) from exc


@dataclass(frozen=True)
class OraIdentifiers:
    """Список имён через запятую: колонки insert или select."""

    names: Sequence[str]

    def __post_init__(self) -> None:
        if not self.names:
            raise QueryBuildError("oracle identifiers: expected at least one name")

    def render(self) -> str:
        rendered: list[str] = []
        for name in self.names:
            rendered.append(OraIdentifier(name).render())

        return ", ".join(rendered)


@dataclass(frozen=True)
class OraLiterals:
    """Список строковых литералов через запятую для `in (...)`; кавычки ставит
    драйвер."""

    values: Sequence[str]

    def __post_init__(self) -> None:
        if not self.values:
            raise QueryBuildError("oracle literals: expected at least one value")

    def render(self) -> str:
        import oracledb  # noqa: PLC0415

        rendered: list[str] = []
        for value in self.values:
            rendered.append(oracledb.enquote_literal(value))

        return ", ".join(rendered)


@dataclass(frozen=True)
class OraBindMarks:
    """Позиционные bind-метки `:1, :2, ...` для `values (...)` по числу колонок."""

    count: int

    def __post_init__(self) -> None:
        if self.count < 1:
            raise QueryBuildError(
                f"oracle bind marks: expected a positive count, got {self.count}"
            )

    def render(self) -> str:
        marks: list[str] = []
        for position in range(1, self.count + 1):
            marks.append(f":{position}")

        return ", ".join(marks)


class OraPiece(Protocol):
    """Кусок запроса, который рендерит драйвер: имя, список имён, литералы,
    bind-метки."""

    def render(self) -> str: ...


OraRendered = OraIdentifier | OraIdentifiers | OraLiterals | OraBindMarks
"""Куски пакета, которые в bind приходить не должны."""


class OraQueryBuilder(QueryBuilder[str]):
    """Реализация QueryBuilder для python-oracledb: куски склеиваются подряд,
    `str` идёт в текст как есть, OraIdentifier, OraIdentifiers, OraLiterals и
    OraBindMarks рендерит драйвер; `:name` в тексте это значение и уезжает
    bind-параметром. Куски одного add стоят вплотную, между вызовами add —
    перенос строки. Текст запроса сборщик не разбирает. Списки Oracle не
    биндит: их подставляют OraLiterals.
    """

    def __init__(self) -> None:
        self._pieces: list[str] = []
        self._params: QueryParams = {}

    def add(self, *pieces: str | OraPiece, **bind: Any) -> Self:
        rendered: list[str] = []
        for piece in pieces:
            if isinstance(piece, str):
                rendered.append(piece)
                continue

            rendered.append(piece.render())

        for name, value in bind.items():
            self._bind(name, value)

        self._pieces.append("".join(rendered))

        return self

    def raw_query(self, text: str, /) -> Self:
        """Стейтмент как есть, без имён и bind'ов"""
        self._pieces.append(text)

        return self

    def when(self, condition: bool, *pieces: str | OraPiece, **bind: Any) -> Self:
        if not condition:
            return self

        return self.add(*pieces, **bind)

    def from_file(self, path: Path, /, **bind: Any) -> Self:
        """Кусок из файла пакета: текст читается целиком и добавляется как add."""
        text = path.read_text(encoding="utf-8")

        return self.add(text, **bind)

    def build(self) -> OraQuery:
        params: QueryParams | None = None
        if self._params:
            params = dict(self._params)

        return AbstractQuery(text="\n".join(self._pieces), params=params)

    def _bind(self, name: str, value: object) -> None:
        if isinstance(value, AbstractQuery):
            msg = (
                f"oracle query builder: {name!r} got a built query; pass its .text "
                "as a piece or bind its values"
            )
            raise QueryBuildError(msg)

        if isinstance(value, OraRendered):
            msg = (
                f"oracle query builder: {name!r} got {type(value).__name__}; pieces "
                "go positionally, bind takes values only"
            )
            raise QueryBuildError(msg)

        if name in self._params and self._params[name] != value:
            msg = (
                f"oracle query builder: parameter {name!r} bound twice with "
                f"different values: {self._params[name]!r} and {value!r}"
            )
            raise QueryBuildError(msg)

        self._params[name] = value
