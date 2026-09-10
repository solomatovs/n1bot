"""Разбор REST-JSON Confluence -> RawDocument.

- ConfluenceJson        — извлечение полей схемы для инструментов чтения.
- ConfluenceJsonDecoder — REST-JSON страницы -> HTML-handle + хэш тела +
  расширенная metadata (title/version/space/ancestors).
- BodyDigest            — хэш тела для реестра источников.

Саму HTML-разметку здесь никто не разбирает: heading-aware extraction поверх
BeautifulSoup живёт в payload'е песочницы (payloads/parse/pages.py).
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import ValidationError

from boba.indexing import (
    ChunkStream,
    Decoder,
    DecoderId,
    RawDocument,
    ReaderKeys,
    TransportKeys,
)
from boba.tool.kb.confluence.models import (
    ConfluenceContent,
    ConfluenceKeys,
    ConfluencePayloadError,
    HttpKeys,
)
from boba.transport.http.profile import HttpConnection

__all__ = ["BodyDigest", "BodyEncoding", "ConfluenceJson", "ConfluenceJsonDecoder"]


class ConfluenceJson:
    """Извлечение полей из REST-JSON Confluence — единое место разбора схемы.

    Все обращения к ключам ответа (title/version/space/ancestors/_links/body/
    results/next) идут через эти @staticmethod'ы: page-decoder, search-reader и
    paginator парсят одну схему одинаково и терпимо к расхождениям версий
    Confluence (отсутствующие/нечисловые/не-dict блоки дают пустой результат).
    """

    @staticmethod
    def as_dict(v: Any) -> dict[str, Any]:
        return v if isinstance(v, dict) else {}

    @staticmethod
    def as_int(v: Any, *, default: int) -> int:
        if v is None:
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def title(data: dict[str, Any]) -> str:
        return str(data.get("title") or "")

    @staticmethod
    def version_number(data: dict[str, Any]) -> int | None:
        n = ConfluenceJson.as_dict(data.get("version")).get("number")
        if n is None:
            return None
        try:
            return int(n)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def last_modified(data: dict[str, Any]) -> str:
        return str(ConfluenceJson.as_dict(data.get("version")).get("when") or "")

    @staticmethod
    def space_key(data: dict[str, Any]) -> str:
        return str(ConfluenceJson.as_dict(data.get("space")).get("key") or "")

    @staticmethod
    def ancestor_titles(data: dict[str, Any]) -> tuple[str, ...]:
        ancestors = data.get("ancestors")
        if not isinstance(ancestors, list):
            return ()
        return tuple(
            str(a.get("title", "")).strip()
            for a in ancestors
            if isinstance(a, dict) and str(a.get("title", "")).strip()
        )

    @staticmethod
    def webui(data: dict[str, Any]) -> str:
        return str(ConfluenceJson.as_dict(data.get("_links")).get("webui") or "")

    @staticmethod
    def body_html(data: dict[str, Any], body_format: str) -> str:
        body = ConfluenceJson.as_dict(data.get("body"))
        block = ConfluenceJson.as_dict(body.get(body_format))
        return str(block.get("value") or "")

    @staticmethod
    def results(data: dict[str, Any]) -> list[dict[str, Any]]:
        res = data.get("results")
        if isinstance(res, list):
            return res
        res = ConfluenceJson.as_dict(data.get("page")).get("results")
        return res if isinstance(res, list) else []

    @staticmethod
    def next_link(data: dict[str, Any]) -> str | None:
        nxt = ConfluenceJson.as_dict(data.get("_links")).get("next")
        return str(nxt) if nxt else None


class ConfluenceJsonDecoder(Decoder):
    """Confluence REST JSON страницы -> HTML-handle + расширенная metadata.

    Вынимает HTML из body.<body_format>.value и считает хэш заголовка с телом
    (TransportKeys.BODY_HASH); обогащает metadata: title (ReaderKeys.PAGE_TITLE),
    version (ConfluenceKeys.VERSION), space, ancestors, source_url,
    last_modified (HttpKeys.LAST_MODIFIED, если ещё не заполнен транспортом).
    """

    DECODER_ID: ClassVar[DecoderId] = DecoderId("ext.confluence_json")

    _HTML_CONTENT_TYPE: ClassVar[str] = "text/html"
    """После JSON->HTML распаковки handle содержит HTML; CONTENT_TYPE приводим к
    этому факту, чтобы DispatchReader мог честно роутить страницы через
    HTMLReader (а не через несуществующий JSONReader)."""

    def __init__(self, *, profile: HttpConnection, body_format: str) -> None:
        self._profile = profile
        self._body_format = body_format

    def decoder_id(self) -> DecoderId:
        return self.DECODER_ID

    async def decode(self, value: RawDocument) -> RawDocument:
        payload = await value.handle.read()
        try:
            content = ConfluenceContent.model_validate_json(payload)
        except ValidationError as e:
            head = payload[:200]
            msg = (
                f"ConfluenceJsonDecoder: decoding page {value.source_id} expected "
                f"a JSON page from Confluence, got {head!r}: {e}"
            )
            raise ConfluencePayloadError(msg) from e

        html = content.body_html(self._body_format).encode(BodyEncoding.UTF8)

        # заголовок сидит в heading_path чанков: переименование меняет хэш
        titled = content.title.encode(BodyEncoding.UTF8) + b"\n" + html
        meta = value.metadata.set(TransportKeys.CONTENT_TYPE, self._HTML_CONTENT_TYPE)
        meta = meta.set(TransportKeys.BODY_HASH, BodyDigest.of(titled))
        if content.title:
            meta = meta.set(ReaderKeys.PAGE_TITLE, content.title)

        meta = meta.set(ConfluenceKeys.VERSION, content.version.number)
        if content.version.when and not meta.has(HttpKeys.LAST_MODIFIED):
            meta = meta.set(HttpKeys.LAST_MODIFIED, content.version.when)

        if content.space.key:
            meta = meta.set(ConfluenceKeys.SPACE_KEY, content.space.key)

        if titles := content.ancestor_titles():
            meta = meta.set(ConfluenceKeys.ANCESTORS_TITLES, titles)

        if content.links.webui:
            url = str(self._profile.url_of(content.links.webui))
            meta = meta.set(ConfluenceKeys.SOURCE_URL, url)

        return replace(value, handle=ChunkStream.of(html), metadata=meta)


class BodyEncoding(StrEnum):
    """Кодировка тел, которые собирает сам транспорт."""

    UTF8 = "utf-8"


class BodyDigest:
    """Хэш тела для реестра: одна функция на страницы и вложения."""

    @staticmethod
    def of(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def new() -> hashlib._Hash:
        return hashlib.sha256()
