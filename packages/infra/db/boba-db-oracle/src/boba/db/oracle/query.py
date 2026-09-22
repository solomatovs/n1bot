"""Сборщик запросов Oracle: текст кусками, `{name}` — имя (закавыченный
идентификатор OraIdentifier или готовый фрагмент OraSql из кода пакета),
`:name` — bind-параметр python-oracledb в словаре.

Ошибки:
QueryBuildError — кусок ждёт плейсхолдер, которому ничего не передано,
    идентификатор содержит кавычку, или один параметр привязан с двумя значениями.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from boba.toolkit.sql import AbstractQuery, QueryBuilder, QueryBuildError, QueryParams

__all__ = ["OraIdentifier", "OraQuery", "OraQueryBuilder", "OraSql"]

OraQuery = AbstractQuery[str, QueryParams | None]
"""Собранный запрос: текст с `:name` плюс словарь bind-параметров."""


@dataclass(frozen=True)
class OraIdentifier:
    """Идентификатор Oracle в двойных кавычках, регистр сохраняется."""

    name: str

    def render(self) -> str:
        if not self.name:
            raise QueryBuildError("oracle identifier: expected a non-empty name")

        if '"' in self.name or "\0" in self.name:
            raise QueryBuildError(
                f"oracle identifier {self.name!r}: quotes and NUL are not allowed"
            )

        return f'"{self.name}"'


@dataclass(frozen=True)
class OraSql:
    """Готовый фрагмент SQL из кода пакета, подставляется как есть."""

    text: str

    def render(self) -> str:
        return self.text


class OraQueryBuilder(QueryBuilder[str]):
    """Реализация QueryBuilder для python-oracledb.

    В куске `{name}` это имя, OraIdentifier или OraSql, `:name` это
    значение и уезжает bind-параметром, литеральные фигурные скобки пишутся
    удвоенными. Списки Oracle не биндит, их подставляют фрагментом OraSql.
    """

    def takes_name(self, value: object) -> bool:
        return isinstance(value, OraIdentifier | OraSql)

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
