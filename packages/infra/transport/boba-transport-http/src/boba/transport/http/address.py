"""Адрес ресурса http(s): сервер плюс путь, который задаёт наследник.

Строка собирается и разбирается только httpx.URL: он экранирует сегменты,
опускает порт схемы по умолчанию в строке и отдаёт path уже раскодированным.
В разобранных полях порт всегда явный. Наследник (Confluence и подобные)
описывает путь ролями: как его собрать из полей и как узнать роли в
чужом пути.

Ошибки:
AddressError — строка не является адресом ресурса (см. boba.connections.address).
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from typing import ClassVar, Self

import httpx
from pydantic import Field, ValidationError

from boba.connections.address import Address, AddressError
from boba.transport.http.profile import UrlScheme

__all__ = ["WebAddress"]


class WebAddress(Address):
    """База адреса ресурса http(s): scheme, host, port; путь — у наследника."""

    BASE_FIELDS: ClassVar[frozenset[str]] = frozenset({"scheme", "host", "port"})
    DEFAULT_PORTS: ClassVar[Mapping[UrlScheme, int]] = {
        UrlScheme.HTTP: 80,
        UrlScheme.HTTPS: 443,
    }

    scheme: UrlScheme
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)

    @abstractmethod
    def path(self) -> str:
        """Путь ресурса из полей наследника, нераскодированный."""

    @classmethod
    @abstractmethod
    def roles_of_path(cls, path: str) -> Mapping[str, str] | None:
        """Роли наследника из раскодированного пути; None — путь не этой формы."""

    def render(self) -> str:
        url = httpx.URL(
            scheme=self.scheme.value,
            host=self.host,
            port=self.port,
            path=self.path(),
        )

        return str(url)

    @classmethod
    def accepts(cls, text: str) -> bool:
        try:
            url = httpx.URL(text)
        except httpx.InvalidURL:
            return False

        if cls._scheme_of(url) is None:
            return False

        return cls.roles_of_path(url.path) is not None

    @classmethod
    def parse(cls, text: str) -> Self:
        url = cls._url_of(text)

        scheme = cls._scheme_of(url)
        if scheme is None:
            msg = (
                f"web address {text!r}: expected scheme http or https, "
                f"got {url.scheme!r}"
            )
            raise AddressError(msg)

        if url.userinfo:
            msg = f"web address {text!r}: credentials are not part of an address"
            raise AddressError(msg)

        if url.query:
            msg = f"web address {text!r}: query is not part of an address"
            raise AddressError(msg)

        if url.fragment:
            msg = f"web address {text!r}: fragment is not part of an address"
            raise AddressError(msg)

        if not url.host:
            msg = f"web address {text!r}: host is required"
            raise AddressError(msg)

        port = url.port
        if port is None:
            port = cls.DEFAULT_PORTS[scheme]

        roles = cls.roles_of_path(url.path)
        if roles is None:
            msg = f"{cls.__name__}: address {text!r} expects path {cls.shape()}"
            raise AddressError(msg)

        parts: dict[str, str | int] = {
            "scheme": scheme.value,
            "host": url.host,
            "port": port,
        }
        parts.update(roles)

        try:
            return cls.model_validate(parts)
        except ValidationError as exc:
            msg = f"{cls.__name__}: address {text!r} is not valid: {exc}"
            raise AddressError(msg) from exc

    @classmethod
    def _url_of(cls, text: str) -> httpx.URL:
        try:
            return httpx.URL(text)
        except httpx.InvalidURL as exc:
            msg = f"web address {text!r}: not a url: {exc}"
            raise AddressError(msg) from exc

    @staticmethod
    def _scheme_of(url: httpx.URL) -> UrlScheme | None:
        """Схема url как член UrlScheme; None — не http и не https."""
        try:
            return UrlScheme(url.scheme)
        except ValueError:
            return None
