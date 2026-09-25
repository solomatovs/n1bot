"""Сборщик запросов ClickHouse: куски текста склеиваются подряд, значения уезжают
параметрами драйвера, и подставляет их только драйвер в одном из своих режимов.
Серверный: в тексте `{name:Type}`, значение уезжает серверу (`{db:Identifier}`,
`{names:Array(String)}`). Клиентский: в тексте `%(name)s`, драйвер сам вписывает
значение перед отправкой — ChIdentifier и ChIdentifiers именами в обратных
кавычках через своё квотирование, строку литералом в кавычках, число как есть;
литеральный процент пишется `%%`. Режим выбирает драйвер: есть хоть один
`{name:Type}` — серверный, иначе клиентский; в одном запросе они не
смешиваются. Текст запроса сборщик не разбирает.

Ошибки:
QueryBuildError — один параметр привязан с двумя значениями, или вместо
    значения пришёл собранный запрос.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from boba.toolkit.sql import AbstractQuery, QueryBuilder, QueryBuildError, QueryParams

__all__ = [
    "ChIdentifier",
    "ChIdentifiers",
    "ChQuery",
    "ChQueryBuilder",
]

ChQuery = AbstractQuery[str, QueryParams | None]
"""Собранный запрос: текст с {name:Type} или %(name)s плюс словарь параметров."""


@dataclass(frozen=True)
class ChIdentifier:
    """Имя базы, таблицы или колонки для клиентского режима: на месте `%(name)s`
    драйвер подставляет str(значение), а квотирует и экранирует имя его же
    quote_identifier. Драйвер импортируется в момент рендера: модуль читает и
    приложение, где драйвера нет."""

    name: str

    def __str__(self) -> str:
        from clickhouse_connect.driver.binding import quote_identifier  # noqa: PLC0415

        return quote_identifier(self.name)


@dataclass(frozen=True)
class ChIdentifiers:
    """Список имён через запятую для клиентского режима: колонки INSERT или SELECT."""

    names: Sequence[str]
    sep: str = ", "

    def __str__(self) -> str:
        rendered: list[str] = []
        for name in self.names:
            rendered.append(str(ChIdentifier(name)))

        return self.sep.join(rendered)


class ChQueryBuilder(QueryBuilder[str]):
    """Реализация QueryBuilder для HTTP-интерфейса ClickHouse: куски текста
    склеиваются подряд, `**bind` целиком уезжает параметрами драйвера. Куски
    одного add стоят вплотную, между вызовами add — перенос строки."""

    def __init__(self) -> None:
        self._pieces: list[str] = []
        self._params: QueryParams = {}

    def add(self, *pieces: str, **bind: Any) -> Self:
        for name, value in bind.items():
            self._bind(name, value)

        self._pieces.append("".join(pieces))

        return self

    def when(self, condition: bool, *pieces: str, **bind: Any) -> Self:
        if not condition:
            return self

        return self.add(*pieces, **bind)

    def read(self, path: Path, /, **bind: Any) -> Self:
        """Кусок из файла пакета: текст читается целиком и добавляется как add."""
        text = path.read_text(encoding="utf-8")

        return self.add(text, **bind)

    def build(self) -> ChQuery:
        params: QueryParams | None = None
        if self._params:
            params = dict(self._params)

        return AbstractQuery(text="\n".join(self._pieces), params=params)

    def _bind(self, name: str, value: object) -> None:
        if isinstance(value, AbstractQuery):
            msg = (
                f"ch query builder: {name!r} got a built query; pass its .text as a "
                "piece or bind its values"
            )
            raise QueryBuildError(msg)

        if name in self._params and self._params[name] != value:
            msg = (
                f"ch query builder: parameter {name!r} bound twice with different "
                f"values: {self._params[name]!r} and {value!r}"
            )
            raise QueryBuildError(msg)

        self._params[name] = value
