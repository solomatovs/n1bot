"""Адреса объектов Confluence по REST-путям поверх WebAddress.

Путь = корень сервиса (root, например `/confluence`, может быть пустым) плюс
хвост объекта: спейс — `/rest/api/space/<space>`, страница —
`/rest/api/content/<page>`, вложение — `/download/attachments/<page>/<file>`.
Разбор — регулярка, якорённая на конец пути; всё до совпадения — root. В
jsonb уходят разобранные роли, а не сырой путь.

Ошибки:
AddressError — строка не является адресом Confluence (см. boba.connections.address).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import PurePosixPath
from typing import ClassVar

from pydantic import Field

from boba.connections.address import AddressFamily
from boba.transport.http.address import WebAddress

__all__ = [
    "ConfluenceAddress",
    "ConfluenceAddresses",
    "ConfluenceAttachmentAddress",
    "ConfluenceNodeKind",
    "ConfluencePageAddress",
    "ConfluenceSpaceAddress",
]


class ConfluenceNodeKind(StrEnum):
    """Виды объектов Confluence, которые адресуются."""

    SPACE = "confluence_space"
    PAGE = "confluence_page"
    ATTACHMENT = "confluence_attachment"


class ConfluenceAddress(WebAddress):
    """База адресов Confluence: корень сервиса и хвост пути по ролям.

    TAIL — регулярка хвоста с именованными группами по ролям наследника,
    PREFIX — статичные сегменты хвоста перед ролями, ROLE_FIELDS — роли в
    порядке сегментов пути.
    """

    TAIL: ClassVar[re.Pattern[str]]
    PREFIX: ClassVar[Sequence[str]]
    ROLE_FIELDS: ClassVar[Sequence[str]]
    SHAPE: ClassVar[str]
    PATH_ROOT: ClassVar[str] = "/"

    root: str = ""

    @classmethod
    def shape(cls) -> str:
        return cls.SHAPE

    def path(self) -> str:
        segments: list[str] = list(self.PREFIX)
        for name in self.ROLE_FIELDS:
            segments.append(getattr(self, name))

        base = self.root
        if not base:
            base = self.PATH_ROOT

        return PurePosixPath(base, *segments).as_posix()

    @classmethod
    def roles_of_path(cls, path: str) -> Mapping[str, str] | None:
        match = cls.TAIL.search(path)
        if match is None:
            return None

        roles: dict[str, str] = {"root": path[: match.start()]}
        roles.update(match.groupdict())

        return roles


class ConfluenceSpaceAddress(ConfluenceAddress):
    KIND = ConfluenceNodeKind.SPACE
    TAIL = re.compile(r"/rest/api/space/(?P<space>[^/]+)$")
    PREFIX = ("rest", "api", "space")
    ROLE_FIELDS = ("space",)
    SHAPE = "{root}/rest/api/space/<space key>"

    space: str = Field(min_length=1)


class ConfluencePageAddress(ConfluenceAddress):
    KIND = ConfluenceNodeKind.PAGE
    TAIL = re.compile(r"/rest/api/content/(?P<page>[^/]+)$")
    PREFIX = ("rest", "api", "content")
    ROLE_FIELDS = ("page",)
    SHAPE = "{root}/rest/api/content/<page id>"

    page: str = Field(min_length=1)


class ConfluenceAttachmentAddress(ConfluenceAddress):
    KIND = ConfluenceNodeKind.ATTACHMENT
    TAIL = re.compile(r"/download/attachments/(?P<page>[^/]+)/(?P<file>[^/]+)$")
    PREFIX = ("download", "attachments")
    ROLE_FIELDS = ("page", "file")
    SHAPE = "{root}/download/attachments/<page id>/<file name>"

    page: str = Field(min_length=1)
    file: str = Field(min_length=1)


class ConfluenceAddresses(AddressFamily):
    """Реестр адресов Confluence."""

    MODELS: ClassVar[Sequence[type[ConfluenceAddress]]] = (
        ConfluenceSpaceAddress,
        ConfluencePageAddress,
        ConfluenceAttachmentAddress,
    )
