"""Сборщик запросов psycopg: куски `sql.SQL`, имена — `sql.Composable`,
значения — `%(name)s` в тексте и словарь параметров.

Ошибки:
QueryBuildError — кусок ждёт плейсхолдер, которому ничего не передано, один
    параметр привязан с двумя значениями, или вместо значения пришёл
    собранный запрос.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, LiteralString, Self, cast

from psycopg import sql

from boba.toolkit.sql import AbstractQuery, QueryBuilder, QueryBuildError, QueryParams

__all__ = ["PgQuery", "PgQueryBuilder"]

PgQuery = AbstractQuery[sql.Composed, QueryParams | None]
"""Собранный запрос: композиция psycopg плюс именованные параметры."""


class PgQueryBuilder(QueryBuilder[sql.Composed]):
    """Реализация QueryBuilder для psycopg.

    В куске `{name}` это имя и подставляется переданным `sql.Composable`
    (`sql.Identifier` для схемы, таблицы, колонки) средствами psycopg,
    `%(name)s` это значение и уезжает параметром драйвера, а литеральные
    фигурные скобки и проценты пишутся удвоенными. Имена из конструктора
    подставляются в каждый кусок. Текст куска приходит из кода или файла
    пакета, не от пользователя, поэтому cast до LiteralString безопасен.
    """

    def __init__(self, **names: sql.Composable) -> None:
        self._names: dict[str, sql.Composable] = dict(names)
        self._pieces: list[sql.Composed] = []
        self._params: QueryParams = {}

    def add(self, text: str, /, **bind: Any) -> Self:
        names: dict[str, sql.Composable] = dict(self._names)
        for name, value in bind.items():
            if isinstance(value, sql.Composable):
                names[name] = value
                continue

            self._bind(name, value)

        self._pieces.append(self._render(text, names))

        return self

    def when(self, condition: bool, text: str, /, **bind: Any) -> Self:
        if not condition:
            return self

        return self.add(text, **bind)

    def read(self, path: Path, /, **bind: Any) -> Self:
        """Кусок из файла пакета: текст читается целиком и добавляется как add."""
        text = path.read_text(encoding="utf-8")

        return self.add(text, **bind)

    def build(self) -> PgQuery:
        params: QueryParams | None = None
        if self._params:
            params = dict(self._params)

        return AbstractQuery(text=sql.SQL("\n").join(self._pieces), params=params)

    def _bind(self, name: str, value: object) -> None:
        if isinstance(value, AbstractQuery):
            msg = (
                f"pg query builder: {name!r} got a built query; pass its .text as a "
                "name or bind its values"
            )
            raise QueryBuildError(msg)

        if name in self._params and self._params[name] != value:
            msg = (
                f"pg query builder: parameter {name!r} bound twice with different "
                f"values: {self._params[name]!r} and {value!r}"
            )
            raise QueryBuildError(msg)

        self._params[name] = value

    def _render(self, text: str, names: Mapping[str, sql.Composable]) -> sql.Composed:
        template = sql.SQL(cast(LiteralString, text))

        try:
            return template.format(**names)
        except (KeyError, ValueError, IndexError) as exc:
            known = ", ".join(sorted(names))
            if not known:
                known = "none"

            msg = (
                f"query piece expects only names {known}, got {exc!r} in {text[:120]!r}"
            )
            raise QueryBuildError(msg) from exc
