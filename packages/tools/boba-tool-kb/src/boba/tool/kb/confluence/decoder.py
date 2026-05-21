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
    RawDocument,
    ReaderKeys,
)
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

        return replace(
            value,
            handle=BytesIO(html.encode("utf-8")),
            metadata=meta,
        )

    def reset(self) -> None:
        pass
