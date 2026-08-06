"""Разбор REST-JSON Confluence -> RawDocument.

- ConfluenceJson        — извлечение полей схемы (title/version/space/...).
- ConfluenceJsonDecoder — REST-JSON -> HTML-handle + расширенная metadata
  (title/version/space/ancestors/attachments).

Саму HTML-разметку здесь никто не разбирает: heading-aware extraction поверх
BeautifulSoup живёт в payload'е песочницы (payloads/parse/pages.py).
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, ClassVar

from boba.indexing import (
    ChunkStream,
    Decoder,
    DecoderId,
    Metadata,
    RawDocument,
    ReaderKeys,
    TransportKeys,
)
from boba.tool.kb.confluence.models import (
    AttachmentInfo,
    ConfluenceKeys,
    ConfluencePayloadError,
    HttpKeys,
)

__all__ = ["ConfluenceJson", "ConfluenceJsonDecoder"]


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
    def source_url(data: dict[str, Any]) -> str:
        links = ConfluenceJson.as_dict(data.get("_links"))
        webui, base = links.get("webui"), links.get("base")
        if webui and base:
            return f"{str(base).rstrip('/')}{webui}"
        return ""

    @staticmethod
    def response_base(data: dict[str, Any]) -> str:
        base = ConfluenceJson.as_dict(data.get("_links")).get("base")
        return str(base).rstrip("/") if base else ""

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
    """Confluence REST JSON -> HTML-handle + расширенная metadata.

    Вынимает HTML из body.<body_format>.value, обогащает metadata: title
    (ReaderKeys.PAGE_TITLE), version (ConfluenceKeys.VERSION), space,
    ancestors, attachments, last_modified (HttpKeys.LAST_MODIFIED, если ещё
    не заполнен HttpTransport'ом).
    """

    DECODER_ID: ClassVar[DecoderId] = DecoderId("ext.confluence_json")

    _HTML_CONTENT_TYPE: ClassVar[str] = "text/html"
    """После JSON->HTML распаковки handle содержит HTML; CONTENT_TYPE приводим к
    этому факту, чтобы DispatchReader мог честно роутить страницы через
    HTMLReader (а не через несуществующий JSONReader)."""

    def __init__(self, *, body_format: str = "export_view") -> None:
        self._body_format = body_format

    def decoder_id(self) -> DecoderId:
        return self.DECODER_ID

    async def decode(self, value: RawDocument) -> RawDocument:
        payload = await value.handle.read()
        if not payload:
            return value
        try:
            data: dict[str, Any] = json.loads(payload)
        except json.JSONDecodeError as e:
            raise ConfluencePayloadError(
                f"ConfluenceJsonDecoder: invalid JSON from Confluence: {e}"
            ) from e
        html = ConfluenceJson.body_html(data, self._body_format)

        meta = value.metadata.set(TransportKeys.CONTENT_TYPE, self._HTML_CONTENT_TYPE)
        if title := ConfluenceJson.title(data):
            meta = meta.set(ReaderKeys.PAGE_TITLE, title)
        if (version := ConfluenceJson.version_number(data)) is not None:
            meta = meta.set(ConfluenceKeys.VERSION, version)
        if (when := ConfluenceJson.last_modified(data)) and not meta.has(
            HttpKeys.LAST_MODIFIED,
        ):
            meta = meta.set(HttpKeys.LAST_MODIFIED, when)
        if space_key := ConfluenceJson.space_key(data):
            meta = meta.set(ConfluenceKeys.SPACE_KEY, space_key)
        if titles := ConfluenceJson.ancestor_titles(data):
            meta = meta.set(ConfluenceKeys.ANCESTORS_TITLES, titles)
        if source_url := ConfluenceJson.source_url(data):
            meta = meta.set(ConfluenceKeys.SOURCE_URL, source_url)
        meta = self._enrich_with_attachments(meta, data)

        return replace(
            value,
            handle=ChunkStream.of(html.encode("utf-8")),
            metadata=meta,
        )

    @staticmethod
    def _enrich_with_attachments(meta: Metadata, data: dict[str, Any]) -> Metadata:
        block = ConfluenceJson.as_dict(
            ConfluenceJson.as_dict(data.get("children")).get("attachment"),
        )
        results = block.get("results")
        if not isinstance(results, list):
            return meta

        items = tuple(
            ConfluenceJsonDecoder._attachment_from_json(a)
            for a in results
            if isinstance(a, dict)
        )
        if not items:
            return meta
        return meta.set(ConfluenceKeys.ATTACHMENTS, items)

    @staticmethod
    def _attachment_from_json(a: dict[str, Any]) -> AttachmentInfo:
        extensions = ConfluenceJson.as_dict(a.get("extensions"))
        version = ConfluenceJson.as_dict(a.get("version"))
        links = ConfluenceJson.as_dict(a.get("_links"))
        return AttachmentInfo(
            id=str(a.get("id", "")),
            title=str(a.get("title", "")),
            media_type=str(extensions.get("mediaType", "")),
            file_size=ConfluenceJson.as_int(extensions.get("fileSize"), default=0),
            download_path=str(links.get("download", "")),
            webui=str(links.get("webui", "")),
            version=ConfluenceJson.as_int(version.get("number"), default=1),
        )
