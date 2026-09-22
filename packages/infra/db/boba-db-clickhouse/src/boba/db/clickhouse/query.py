"""Сборщик запросов ClickHouse: текст кусками, подстановку делает сервер по
`{name:Type}` из словаря параметров, идентификатор — `{name:Identifier}`.

Ошибки:
QueryBuildError — один параметр привязан с двумя значениями.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from boba.toolkit.sql import AbstractQuery, QueryBuilder, QueryParams

__all__ = ["ChQuery", "ChQueryBuilder"]

ChQuery = AbstractQuery[str, QueryParams | None]
"""Собранный запрос: текст с {name:Type} плюс словарь параметров."""


class ChQueryBuilder(QueryBuilder[str]):
    """Реализация QueryBuilder для HTTP-интерфейса ClickHouse: имён на стороне
    клиента нет, все значения уезжают серверными параметрами, текст куска
    остаётся как есть."""

    def takes_name(self, value: object) -> bool:
        return False

    def render_piece(self, text: str, names: Mapping[str, Any]) -> str:
        return text

    def join_pieces(self, pieces: Sequence[str]) -> str:
        return "\n".join(pieces)
