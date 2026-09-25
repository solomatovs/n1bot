"""Сборщик запросов ClickHouse

Строка (`str`), привязанная к куску, — голый фрагмент текста: встаёт на место
`$name` без преобразований, литеральный доллар в таком куске пишется `$$`.
Куски без строковых имён билдер не трогает. Любое другое значение — параметр
драйвера, и подставляет его драйвер в одном из своих режимов. Серверный: в
тексте `{name:Type}`, значение уезжает серверу (`{db:Identifier}`,
`{names:Array(String)}`). Клиентский: в тексте `%(name)s`, драйвер сам
вписывает значение перед отправкой — ChIdentifier и ChIdentifiers именами в
обратных кавычках через своё квотирование, строку литералом в кавычках, число
как есть; литеральный процент пишется `%%`. Режим выбирает драйвер: есть хоть
один `{name:Type}` — серверный, иначе клиентский; в одном запросе они не
смешиваются. Строковое значение параметра оборачивается в ChValue, иначе оно
станет голым текстом.

Ошибки:
QueryBuildError — кусок ждёт `$name`, которому ничего не передано, или один
    параметр привязан с двумя значениями.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from string import Template
from typing import Any

from boba.toolkit.sql import AbstractQuery, QueryBuilder, QueryBuildError, QueryParams

__all__ = [
    "ChIdentifier",
    "ChIdentifiers",
    "ChQuery",
    "ChQueryBuilder",
    "ChValue",
]

ChQuery = AbstractQuery[str, QueryParams | None]
"""Собранный запрос: текст с {name:Type} или %(name)s плюс словарь параметров."""


@dataclass(frozen=True)
class ChValue:
    """Значение параметра драйвера, в том числе строка: билдер кладёт в параметры
    само значение, а подставляет его драйвер — серверу `{name:Type}` или
    литералом на месте `%(name)s`."""

    value: object


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
    """Реализация QueryBuilder для HTTP-интерфейса ClickHouse: строки встают в
    текст куска на место `$name`, всё остальное уезжает параметрами, которые
    подставляет драйвер."""

    def takes_name(self, value: object) -> bool:
        return isinstance(value, str)

    def param_value(self, value: object) -> object:
        if isinstance(value, ChValue):
            return value.value

        return value

    def render_piece(self, text: str, names: Mapping[str, Any]) -> str:
        if not names:
            return text

        try:
            return Template(text).substitute(names)
        except (KeyError, ValueError) as exc:
            known = ", ".join(sorted(names))
            msg = (
                f"query piece expects only names {known} as $name, got {exc!r} "
                f"in {text[:120]!r}"
            )
            raise QueryBuildError(msg) from exc

    def join_pieces(self, pieces: Sequence[str]) -> str:
        return "\n".join(pieces)
