"""Сборщик запросов Oracle: текст кусками, `{name}` — имя, `:name` — bind-параметр
python-oracledb в словаре.

Имена квотирует драйвер по своим правилам для идентификаторов и литералов:
OraIdentifier — простое или составное имя (HR.EMPLOYEES, obj$, x@link) идёт как
есть и регистр решает сервер, любое другое (пробел, точка в имени, цифра в
начале) уезжает в двойных кавычках как написано; OraIdentifiers — список имён
через запятую; OraLiterals — список строковых литералов для `in (...)`;
OraBindMarks — позиционные метки `:1, :2, ...` по числу колонок; OraSql —
готовый фрагмент из кода пакета как есть. Драйвер импортируется в момент
рендера: модуль читает и приложение, где драйвера нет.

Ошибки:
QueryBuildError — кусок ждёт плейсхолдер, которому ничего не передано,
    имя пустое или с кавычкой внутри, или один параметр привязан с двумя значениями.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from boba.toolkit.sql import AbstractQuery, QueryBuilder, QueryBuildError, QueryParams

__all__ = [
    "OraBindMarks",
    "OraIdentifier",
    "OraIdentifiers",
    "OraLiterals",
    "OraQuery",
    "OraQueryBuilder",
    "OraSql",
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

    SEPARATOR: ClassVar[str] = ", "

    names: Sequence[str]

    def __post_init__(self) -> None:
        if not self.names:
            raise QueryBuildError("oracle identifiers: expected at least one name")

    def render(self) -> str:
        rendered: list[str] = []
        for name in self.names:
            rendered.append(OraIdentifier(name).render())

        return self.SEPARATOR.join(rendered)


@dataclass(frozen=True)
class OraLiterals:
    """Список строковых литералов через запятую для `in (...)`; кавычки ставит
    драйвер."""

    SEPARATOR: ClassVar[str] = ", "

    values: Sequence[str]

    def __post_init__(self) -> None:
        if not self.values:
            raise QueryBuildError("oracle literals: expected at least one value")

    def render(self) -> str:
        import oracledb  # noqa: PLC0415

        rendered: list[str] = []
        for value in self.values:
            rendered.append(oracledb.enquote_literal(value))

        return self.SEPARATOR.join(rendered)


@dataclass(frozen=True)
class OraBindMarks:
    """Позиционные bind-метки `:1, :2, ...` для `values (...)` по числу колонок."""

    SEPARATOR: ClassVar[str] = ", "

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

        return self.SEPARATOR.join(marks)


@dataclass(frozen=True)
class OraSql:
    """Готовый фрагмент SQL из кода пакета, подставляется как есть."""

    text: str

    def render(self) -> str:
        return self.text


class OraQueryBuilder(QueryBuilder[str]):
    """Реализация QueryBuilder для python-oracledb.

    В куске `{name}` это имя — OraIdentifier, OraIdentifiers, OraLiterals,
    OraBindMarks или OraSql, `:name` это значение и уезжает bind-параметром,
    литеральные фигурные скобки пишутся удвоенными. Списки Oracle не биндит:
    их подставляют OraLiterals или фрагментом OraSql.
    """

    def takes_name(self, value: object) -> bool:
        return isinstance(
            value,
            OraIdentifier | OraIdentifiers | OraLiterals | OraBindMarks | OraSql,
        )

    def render_piece(self, text: str, names: Mapping[str, Any]) -> str:
        rendered: dict[str, str] = {}
        for name, value in names.items():
            rendered[name] = value.render()

        try:
            return text.format(**rendered)
        except (KeyError, ValueError, IndexError) as exc:
            known = ", ".join(sorted(rendered))
            if not known:
                known = "none"

            msg = (
                f"query piece expects only names {known}, got {exc!r} in {text[:120]!r}"
            )
            raise QueryBuildError(msg) from exc

    def join_pieces(self, pieces: Sequence[str]) -> str:
        return "\n".join(pieces)
