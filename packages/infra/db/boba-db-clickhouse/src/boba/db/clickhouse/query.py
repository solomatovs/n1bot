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
QueryBuildError — кусок ждёт `$name`, которому ничего не передано, один
    параметр привязан с двумя значениями, или вместо значения пришёл
    собранный запрос.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any, Self

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
    подставляет драйвер. Строки из конструктора подставляются в каждый кусок."""

    def __init__(self, **names: str) -> None:
        for name, value in names.items():
            if isinstance(value, str):
                continue

            msg = (
                f"ch query builder: standing name {name!r} expects a str, got {value!r}"
            )
            raise QueryBuildError(msg)

        self._names: dict[str, str] = dict(names)
        self._pieces: list[str] = []
        self._params: QueryParams = {}

    def add(self, text: str, /, **bind: Any) -> Self:
        names: dict[str, str] = dict(self._names)
        for name, value in bind.items():
            if isinstance(value, str):
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

    def build(self) -> ChQuery:
        params: QueryParams | None = None
        if self._params:
            params = dict(self._params)

        return AbstractQuery(text="\n".join(self._pieces), params=params)

    def _bind(self, name: str, value: object) -> None:
        if isinstance(value, AbstractQuery):
            msg = (
                f"ch query builder: {name!r} got a built query; pass its .text as a "
                "name or bind its values"
            )
            raise QueryBuildError(msg)

        bound = value
        if isinstance(value, ChValue):
            bound = value.value

        if name in self._params and self._params[name] != bound:
            msg = (
                f"ch query builder: parameter {name!r} bound twice with different "
                f"values: {self._params[name]!r} and {bound!r}"
            )
            raise QueryBuildError(msg)

        self._params[name] = bound

    def _render(self, text: str, names: Mapping[str, str]) -> str:
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
