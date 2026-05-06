"""ConfluenceJsonDecoder: REST-JSON → RawDocument(html-handle, +title/version).

Confluence Content REST endpoint возвращает JSON вида::

    {
      "id": "123",
      "title": "Page Title",
      "body": {"<format>": {"value": "<html>...</html>"}},
      "version": {"number": 7, "when": "2024-01-..."}
    }

Decoder вынимает HTML из `body.<body_format>.value`, кладёт его в новый
handle (BytesIO), обогащает metadata: `title`, `version` (число → str),
`last_modified` (когда отсутствует — из `version.when`). Reader дальше
работает с готовым HTML и `metadata['title']`.
"""

from __future__ import annotations

import json
from dataclasses import replace
from io import BytesIO
from typing import Any

from boba.processing import Decoder, DecoderId, IndexingContext, RawDocument

__all__ = ["ConfluenceJsonDecoder"]

_DECODER_ID = DecoderId("ext.confluence_json")
_HTML_HINT = "confluence_html"


class ConfluenceJsonDecoder(Decoder):
    """Confluence REST JSON → HTML-handle + расширенная metadata."""

    def __init__(self, *, body_format: str = "export_view") -> None:
        self._body_format = body_format

    def name(self) -> str:
        return f"ConfluenceJsonDecoder(format={self._body_format})"

    def decoder_id(self) -> DecoderId:
        return _DECODER_ID

    def convert(
        self, ctx: IndexingContext, value: RawDocument,
    ) -> RawDocument:
        del ctx
        payload = value.handle.read()
        if not payload:
            return value
        data: dict[str, Any] = json.loads(payload)
        body = data.get("body") or {}
        body_block = body.get(self._body_format) or {}
        html = str(body_block.get("value", "")) if isinstance(body_block, dict) else ""

        merged: dict[str, str] = {**value.metadata}
        if title := data.get("title"):
            merged["title"] = str(title)
        version = data.get("version") or {}
        if isinstance(version, dict):
            if (n := version.get("number")) is not None:
                merged["version"] = str(n)
            if (when := version.get("when")) and "last_modified" not in merged:
                merged["last_modified"] = str(when)

        return replace(
            value,
            handle=BytesIO(html.encode("utf-8")),
            content_hint=_HTML_HINT,
            metadata=merged,
        )

    def reset(self) -> None:
        pass
