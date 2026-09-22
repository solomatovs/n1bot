"""Сборщик запросов psycopg: куски `sql.SQL`, имена — `sql.Composable`,
значения — `%(name)s` в тексте и словарь параметров.

Ошибки:
QueryBuildError — кусок ждёт плейсхолдер, которому ничего не передано, или один
    параметр привязан с двумя значениями.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, LiteralString, cast

from psycopg import sql

from boba.toolkit.sql import AbstractQuery, QueryBuilder, QueryBuildError, QueryParams

__all__ = ["PgQuery", "PgQueryBuilder"]

PgQuery = AbstractQuery[sql.Composed, QueryParams | None]
"""Собранный запрос: композиция psycopg плюс именованные параметры."""


class PgQueryBuilder(QueryBuilder[sql.Composed]):
    """Реализация QueryBuilder для psycopg.

    В куске `{name}` это имя и подставляется переданным `sql.Composable`
    (`sql.Identifier` для схемы, таблицы, колонки), `%(name)s` это значение и
    уезжает параметром драйвера, а литеральные фигурные скобки и проценты
    пишутся удвоенными. Текст куска приходит из кода или файла пакета, не от
    пользователя, поэтому cast до LiteralString безопасен.
    """

    def takes_name(self, value: object) -> bool:
        return isinstance(value, sql.Composable)

    def render_piece(self, text: str, names: Mapping[str, Any]) -> sql.Composed:
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

    def join_pieces(self, pieces: Sequence[sql.Composed]) -> sql.Composed:
        return sql.SQL("\n").join(pieces)
