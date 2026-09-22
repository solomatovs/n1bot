"""Формулы ссылок {schema}.surface_url: как из адреса node собрать строку, по
которой объект открывает человек.

Адрес лежит частями (scheme, host, port, path, database, schema...), а строка
собирается из них по-разному, и правило знает владелец поверхности. Формула это
строка с подстановками `{ключ}` в нотации str.format: ключ это часть адреса в
percent-кодировке (косая черта сохраняется), сверх них есть `{origin}` — scheme://host
с портом, если он не порт схемы. Кусок в `[...]` необязателен: нет хоть одной
подстановки внутри, кусок исчезает целиком. Так одной формулой пишется и страница
Confluence под префиксом сервера, и колонка PostgreSQL, которая живёт то в таблице,
то в представлении:

    cfl_page        {origin}{path}/pages/viewpage.action?pageId={content}
    pg_meta_column  {origin}/{database}?schema={schema}[&table={table}][&view={view}]
                    &column={column}

Формулы читает IxRegistry, разбирает UrlTemplate стандартным string.Formatter.

Ошибки:
SurfaceUrlError — формула не разбирается: пустая, непарные скобки, подстановка не
    из строчных букв, цифр и подчёркивания.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from string import Formatter
from typing import Any
from urllib.parse import quote

__all__ = ["SurfaceUrl", "SurfaceUrlError", "UrlFragment", "UrlTemplate"]


class SurfaceUrlError(Exception):
    """Формула ссылки не разбирается."""


@dataclass(frozen=True, kw_only=True)
class UrlFragment:
    """Кусок формулы: текст с подстановками и признак необязательности."""

    text: str
    keys: tuple[str, ...]
    optional: bool

    def render(self, address: Mapping[str, Any]) -> str:
        if self.optional:
            for key in self.keys:
                if not self._known(key, address):
                    return ""

        parts: list[str] = []
        for literal, key, _, _ in Formatter().parse(self.text):
            parts.append(literal)
            if key is not None:
                parts.append(self._value(key, address))

        return "".join(parts)

    def _known(self, key: str, address: Mapping[str, Any]) -> bool:
        if key == "origin":
            return bool(self._origin(address))

        return address.get(key) is not None

    def _value(self, key: str, address: Mapping[str, Any]) -> str:
        if key == "origin":
            return self._origin(address)

        found = address.get(key)
        if found is None:
            return ""

        return quote(str(found), safe="/")

    def _origin(self, address: Mapping[str, Any]) -> str:
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


class UrlTemplate:
    """Разобранная формула: куски по порядку, необязательные в `[...]`."""

    def __init__(self, template: str) -> None:
        cleaned = template.strip()
        if not cleaned:
            raise SurfaceUrlError("url template: expected a template, got empty string")

        self._template = cleaned
        self._fragments = tuple(self._split(cleaned))

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

    def _split(self, template: str) -> list[UrlFragment]:
        """Куски по скобкам `[...]`: текст снаружи обязателен, внутри — нет."""
        fragments: list[UrlFragment] = []
        chunk = ""
        optional = False
        for char in template:
            if char == "[":
                if optional:
                    raise SurfaceUrlError(
                        f"url template {template!r}: nested [ inside an optional chunk"
                    )

                if chunk:
                    fragments.append(self._fragment(chunk, optional=False))

                chunk = ""
                optional = True
                continue

            if char == "]":
                if not optional:
                    raise SurfaceUrlError(
                        f"url template {template!r}: ] without an opening ["
                    )

                fragments.append(self._fragment(chunk, optional=True))
                chunk = ""
                optional = False
                continue

            chunk += char

        if optional:
            raise SurfaceUrlError(f"url template {template!r}: [ without a closing ]")

        if chunk:
            fragments.append(self._fragment(chunk, optional=False))

        return fragments

    def _fragment(self, text: str, *, optional: bool) -> UrlFragment:
        keys: list[str] = []
        try:
            pieces = list(Formatter().parse(text))
        except ValueError as exc:
            raise SurfaceUrlError(f"url template chunk {text!r}: {exc}") from exc

        for _, key, spec, conversion in pieces:
            if key is None:
                continue

            if spec or conversion or not key.isidentifier() or not key.islower():
                raise SurfaceUrlError(
                    f"url template chunk {text!r}: expected {{name}} with a lowercase "
                    f"identifier inside, got {key!r}"
                )

            keys.append(key)

        return UrlFragment(text=text, keys=tuple(keys), optional=optional)


@dataclass(frozen=True, kw_only=True)
class SurfaceUrl:
    """Одна строка {schema}.surface_url."""

    surface: str
    template: str
    owner: str
