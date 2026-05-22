"""ConfluenceJsonDecoder: REST-JSON → RawDocument(html-handle, +title/version).

Confluence возвращает JSON вида::

    {
      "id": "123",
      "title": "Page Title",
      "body": {"<format>": {"value": "<html>...</html>"}},
      "version": {"number": 7, "when": "2024-01-..."}
    }

Decoder вынимает HTML из `body.<body_format>.value`, кладёт его в новый
handle (BytesIO), обогащает metadata: title (`ReaderKeys.PAGE_TITLE`),
version (`ConfluenceKeys.VERSION`), last_modified (`HttpKeys.LAST_MODIFIED`,
если ещё не заполнен HttpTransport'ом). Reader дальше работает с готовым
HTML.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import replace
from io import BytesIO
from typing import Any

from boba.indexing import (
    Decoder,
    DecoderId,
    Metadata,
    RawDocument,
    ReaderKeys,
)
from boba.tool.kb.confluence.attachments import AttachmentInfo
from boba.tool.kb.confluence.errors import ConfluencePayloadError
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.transport.http import HttpKeys

__all__ = ["ConfluenceJsonDecoder"]


class ConfluenceJsonDecoder(Decoder):
    """Confluence REST JSON → HTML-handle + расширенная metadata."""

    DECODER_ID = DecoderId("ext.confluence_json")

    def __init__(self, *, body_format: str = "export_view") -> None:
        self._body_format = body_format

    def name(self) -> str:
        return f"ConfluenceJsonDecoder(format={self._body_format})"

    def decoder_id(self) -> DecoderId:
        return self.DECODER_ID

    def convert(self, value: RawDocument) -> RawDocument:
        payload = value.handle.read()
        if not payload:
            return value
        try:
            data: dict[str, Any] = json.loads(payload)
        except json.JSONDecodeError as e:
            raise ConfluencePayloadError(
                f"ConfluenceJsonDecoder: невалидный JSON от Confluence: {e}"
            ) from e
        body = data.get("body") or {}
        body_block = body.get(self._body_format) or {}
        html = str(body_block.get("value", "")) if isinstance(body_block, dict) else ""

        meta = value.metadata
        if title := data.get("title"):
            meta = meta.set(ReaderKeys.PAGE_TITLE, str(title))
        version = data.get("version") or {}
        if isinstance(version, dict):
            if (n := version.get("number")) is not None:
                with contextlib.suppress(TypeError, ValueError):
                    meta = meta.set(ConfluenceKeys.VERSION, int(n))
            if (when := version.get("when")) and not meta.has(HttpKeys.LAST_MODIFIED):
                meta = meta.set(HttpKeys.LAST_MODIFIED, str(when))
        space = data.get("space") or {}
        if isinstance(space, dict) and (space_key := space.get("key")):
            meta = meta.set(ConfluenceKeys.SPACE_KEY, str(space_key))
        ancestors = data.get("ancestors")
        if isinstance(ancestors, list):
            titles = tuple(
                str(a.get("title", "")).strip()
                for a in ancestors
                if isinstance(a, dict) and str(a.get("title", "")).strip()
            )
            if titles:
                meta = meta.set(ConfluenceKeys.ANCESTORS_TITLES, titles)
        meta = _enrich_with_attachments(meta, data)

        return replace(
            value,
            handle=BytesIO(html.encode("utf-8")),
            metadata=meta,
        )

    def reset(self) -> None:
        pass


def _enrich_with_attachments(meta: Metadata, data: dict[str, Any]) -> Metadata:
    """Put `children.attachment.results[]` into `ConfluenceKeys.ATTACHMENTS`.

    Пустой/отсутствующий список → `meta` без изменений (тот же pattern,
    что у `ANCESTORS_TITLES`). Структурно непригодный JSON тоже даёт no-op:
    Decoder не должен взрываться на расхождении схемы — Confluence в
    разных версиях кладёт expand-блоки по-разному.
    """
    children = data.get("children")
    if not isinstance(children, dict):
        return meta
    block = children.get("attachment")
    if not isinstance(block, dict):
        return meta
    results = block.get("results")
    if not isinstance(results, list):
        return meta
    items = tuple(
        _attachment_from_json(a)
        for a in results
        if isinstance(a, dict)
    )
    if not items:
        return meta
    return meta.set(ConfluenceKeys.ATTACHMENTS, items)


def _attachment_from_json(a: dict[str, Any]) -> AttachmentInfo:
    """Один Confluence-attachment JSON-объект → `AttachmentInfo`.

    Missing/нечисловые `extensions.fileSize` и `version.number` фолбэчатся
    в `0` и `1` — пользователю download'а размер не критичен, version по
    умолчанию `1` соответствует Confluence-семантике первой загрузки.
    """
    extensions: dict[str, Any] = _dict_or_empty(a.get("extensions"))
    version: dict[str, Any] = _dict_or_empty(a.get("version"))
    links: dict[str, Any] = _dict_or_empty(a.get("_links"))
    return AttachmentInfo(
        id=str(a.get("id", "")),
        title=str(a.get("title", "")),
        media_type=str(extensions.get("mediaType", "")),
        file_size=_int_or(extensions.get("fileSize"), default=0),
        download_path=str(links.get("download", "")),
        version=_int_or(version.get("number"), default=1),
    )


def _dict_or_empty(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _int_or(v: Any, *, default: int) -> int:
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default
