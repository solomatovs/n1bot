"""Формулы ссылок на объекты: шаблон владельца поверхности и его применение.

Адрес node это части в jsonb, а строка, по которой объект открывает человек или
называет модель, собирается из них по правилу происхождения. Правило живёт строкой в
{schema}.surface_url, владелец кладёт его своим файлом схемы, а потребитель читает
реестр один раз при старте и применяет шаблон к каждой выдаче, не зная ни
происхождений, ни поверхностей.

Грамматика шаблона:
`{ключ}` — подстановка: значение части адреса в percent-кодировке; слэш в значении
    сохраняется, потому что подстановкой бывает путь. Сверх ключей адреса доступен
    `{origin}` — scheme://host с портом, если он не порт схемы.
`[...]` — необязательный кусок: если хоть одной подстановки внутри в адресе нет,
    кусок исчезает целиком. Так одной формулой пишется колонка, которая живёт и в
    таблице, и в представлении: `?schema={schema}[&table={table}][&view={view}]`.
Подстановка вне куска, которой нет в адресе, даёт пустую строку.

Ошибки:
SurfaceUrlError — шаблон не разбирается: пустой, подстановка названа не
    идентификатором или скобка непарная.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, ClassVar
from urllib.parse import quote

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict

from boba.db.postgres.query import PgQueryBuilder

__all__ = [
    "SurfaceUrl",
    "SurfaceUrlError",
    "SurfaceUrls",
    "UrlFragment",
    "UrlTemplate",
]


class SurfaceUrlError(Exception):
    """Формула ссылки не разбирается."""


class AddressPart:
    """Части адреса, из которых собирается origin шаблона."""


class UrlValue:
    """Значение одной подстановки: часть адреса или собранный origin."""

    @classmethod
    def known(cls, name: str, address: Mapping[str, Any]) -> bool:
        """Есть ли подстановка в адресе; origin есть, когда есть сервер."""
        if name == "origin":
            return bool(cls._origin(address))

        return address.get(name) is not None

    @classmethod
    def of(cls, name: str, address: Mapping[str, Any]) -> str:
        if name == "origin":
            return cls._origin(address)

        found = address.get(name)
        if found is None:
            return ""

        return quote(str(found), safe="/")

    @classmethod
    def _origin(cls, address: Mapping[str, Any]) -> str:
        scheme = str(address.get("scheme") or "")
        host = str(address.get("host") or "")
        if not scheme:
            return ""

        if not host:
            return ""

        origin = f"{scheme}://{host}"
        port = address.get("port")
        if port is None:
            return origin

        if {"http": 80, "https": 443}.get(scheme) == int(port):
            return origin

        return f"{origin}:{int(port)}"


class UrlFragment(BaseModel):
    """Кусок шаблона: текст с подстановками и признак необязательности."""

    model_config = ConfigDict(frozen=True)

    text: str
    keys: tuple[str, ...]
    optional: bool

    def render(self, address: Mapping[str, Any]) -> str:
        if self.optional:
            absent = self._absent(address)
            if absent:
                return ""

        return TemplateSyntax.PLACEHOLDER.sub(
            lambda match: UrlValue.of(match.group(1), address), self.text
        )

    def _absent(self, address: Mapping[str, Any]) -> str:
        """Первая подстановка куска, которой в адресе нет; пусто — есть все."""
        for key in self.keys:
            if not UrlValue.known(key, address):
                return key

        return ""


class TemplateSyntax:
    """Разбор строки формулы на куски."""

    PLACEHOLDER: ClassVar[re.Pattern[str]] = re.compile(r"\{([a-z_][a-z0-9_]*)\}")

    @classmethod
    def parse(cls, template: str) -> tuple[UrlFragment, ...]:
        fragments: list[UrlFragment] = []
        position = 0
        for group in re.compile(r"\[([^\[\]]*)\]").finditer(template):
            before = template[position : group.start()]
            if before:
                fragments.append(cls._fragment(before, optional=False))

            fragments.append(cls._fragment(group.group(1), optional=True))
            position = group.end()

        tail = template[position:]
        if tail:
            fragments.append(cls._fragment(tail, optional=False))

        return tuple(fragments)

    @classmethod
    def _fragment(cls, text: str, *, optional: bool) -> UrlFragment:
        keys: list[str] = []
        for match in cls.PLACEHOLDER.finditer(text):
            keys.append(match.group(1))

        leftover = cls.PLACEHOLDER.sub("", text)
        for bracket in "{}[]":
            if bracket in leftover:
                raise SurfaceUrlError(
                    f"url template chunk {text!r}: expected {{name}} with a lowercase "
                    f"identifier inside and paired [] around an optional chunk, "
                    f"got a stray {bracket!r}"
                )

        return UrlFragment(text=text, keys=tuple(keys), optional=optional)


class UrlTemplate:
    """Формула ссылки одной поверхности и её применение к адресу."""

    def __init__(self, template: str) -> None:
        cleaned = template.strip()
        if not cleaned:
            raise SurfaceUrlError("url template: expected a template, got empty string")

        self._template = cleaned
        self._fragments = TemplateSyntax.parse(cleaned)

    @property
    def text(self) -> str:
        return self._template

    def placeholders(self) -> tuple[str, ...]:
        keys: list[str] = []
        for fragment in self._fragments:
            keys.extend(fragment.keys)

        return tuple(keys)

    def render(self, address: Mapping[str, Any]) -> str:
        """Ссылка по адресу: куски по порядку, необязательный без своих частей пуст."""
        parts: list[str] = []
        for fragment in self._fragments:
            parts.append(fragment.render(address))

        return "".join(parts)


class SurfaceUrl(BaseModel):
    """Одна строка {schema}.surface_url."""

    model_config = ConfigDict(frozen=True)

    surface: str
    template: str
    owner: str


class SurfaceUrls:
    """Чтение формул из реестра и их применение к выдаче."""

    def __init__(self, templates: Mapping[str, UrlTemplate]) -> None:
        self._templates = dict(templates)

    @classmethod
    async def load(
        cls, conn: psycopg.AsyncConnection[Any], db_schema: str
    ) -> SurfaceUrls:
        query = (
            PgQueryBuilder()
            .add(
                """
                select
                    u.surface::varchar,
                    u.template,
                    u.owner
                from
                    {schema}.surface_url u
                order by
                    u.surface
            """,
                schema=sql.Identifier(db_schema),
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)
        rows = await cur.fetchall()

        templates: dict[str, UrlTemplate] = {}
        for surface, template, _ in rows:
            try:
                templates[str(surface)] = UrlTemplate(str(template))
            except SurfaceUrlError as exc:
                raise SurfaceUrlError(f"surface {surface}: {exc}") from exc

        return cls(templates)

    @classmethod
    def rows(cls, raw: Iterable[Sequence[Any]]) -> list[SurfaceUrl]:
        found: list[SurfaceUrl] = []
        for surface, template, owner in raw:
            found.append(
                SurfaceUrl(
                    surface=str(surface), template=str(template), owner=str(owner)
                )
            )

        return found

    def surfaces(self) -> tuple[str, ...]:
        return tuple(sorted(self._templates))

    def of(self, surface: str, address: Mapping[str, Any]) -> str:
        """Ссылка на объект; у поверхности без формулы её нет, и это пустая строка."""
        template = self._templates.get(surface)
        if template is None:
            return ""

        return template.render(address)
