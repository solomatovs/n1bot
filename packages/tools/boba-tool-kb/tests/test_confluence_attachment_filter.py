"""`AttachmentFilter` + интеграция с `_iter_attachments`/`ConfluenceContentTransport`.

Покрываем:
- passthrough (пустые списки) — пропускает всё.
- allowlist по `media_type` — только matching.
- allowlist по `title` — только matching.
- OR между списками — match либо в media_type, либо в title.
- case-insensitive.
- `_iter_attachments` не делает HTTP за отсеянные вложения (lazy + skip).
- `ConfluenceContentTransport` уважает filter на fan-out стадии.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from io import BytesIO

from boba.indexing import (
    Metadata,
    PipelineContext,
    PipelineId,
    RawDocument,
    SourceId,
    Transport,
    TransportKeys,
)
from boba.tool.kb.confluence._pipeline_common import (
    ConfluenceContentTransport,
    _iter_attachments,
)
from boba.tool.kb.confluence.attachments import AttachmentFilter, AttachmentInfo
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.transport.http import HttpRequest

_ATT_PNG = AttachmentInfo(
    id="att-1",
    title="diagram.png",
    media_type="image/png",
    file_size=10,
    download_path="/download/attachments/42/diagram.png?version=1",
    version=1,
)
_ATT_PDF = AttachmentInfo(
    id="att-2",
    title="spec.pdf",
    media_type="application/pdf",
    file_size=20,
    download_path="/download/attachments/42/spec.pdf?version=1",
    version=1,
)
_ATT_DOCX = AttachmentInfo(
    id="att-3",
    title="Report-Q1.DOCX",
    media_type=(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    file_size=30,
    download_path="/download/attachments/42/Report-Q1.DOCX?version=1",
    version=1,
)


# -------- AttachmentFilter unit --------


def test_passthrough_when_both_lists_empty() -> None:
    flt = AttachmentFilter()
    assert flt.is_passthrough()
    assert flt.matches(_ATT_PNG)
    assert flt.matches(_ATT_PDF)
    assert flt.matches(_ATT_DOCX)


def test_media_type_allowlist_exact() -> None:
    flt = AttachmentFilter.from_lists(media_types=["application/pdf"])
    assert not flt.is_passthrough()
    assert flt.matches(_ATT_PDF)
    assert not flt.matches(_ATT_PNG)
    assert not flt.matches(_ATT_DOCX)


def test_media_type_allowlist_glob() -> None:
    flt = AttachmentFilter.from_lists(media_types=["image/*"])
    assert flt.matches(_ATT_PNG)
    assert not flt.matches(_ATT_PDF)


def test_title_allowlist_glob() -> None:
    flt = AttachmentFilter.from_lists(titles=["*.pdf", "*.docx"])
    assert flt.matches(_ATT_PDF)
    assert flt.matches(_ATT_DOCX)  # .DOCX (case-insensitive)
    assert not flt.matches(_ATT_PNG)


def test_or_between_lists() -> None:
    """attachment проходит, если матчит ХОТЯ БЫ один паттерн в любом списке."""
    flt = AttachmentFilter.from_lists(
        media_types=["application/pdf"],
        titles=["*.png"],
    )
    assert flt.matches(_ATT_PDF)   # via media_type
    assert flt.matches(_ATT_PNG)   # via title
    assert not flt.matches(_ATT_DOCX)


def test_case_insensitive_media_type() -> None:
    """Паттерны и значения сравниваются в lower-case."""
    flt = AttachmentFilter.from_lists(media_types=["APPLICATION/PDF"])
    assert flt.matches(_ATT_PDF)


# -------- _iter_attachments integration --------


def _parent_with(attachments: tuple[AttachmentInfo, ...]) -> RawDocument:
    meta = (
        Metadata.empty()
        .set(ConfluenceKeys.PAGE_ID, "42")
        .set(ConfluenceKeys.HOST, "confl.example.com")
        .set(ConfluenceKeys.ATTACHMENTS, attachments)
    )
    return RawDocument(
        handle=BytesIO(b"<html>page</html>"),
        source_id=SourceId(
            "https://confl.example.com/wiki/pages/viewpage.action?pageId=42"
        ),
        metadata=meta,
    )


def _pctx() -> PipelineContext:
    return PipelineContext(pipeline_id=PipelineId("test"))


class _FakeTransport(Transport[HttpRequest]):
    def __init__(self) -> None:
        self.seen: list[HttpRequest] = []

    def name(self) -> str:
        return "FakeTransport"

    def stream(
        self,
        ctx: PipelineContext,
        stream: Iterable[HttpRequest],
    ) -> Iterable[RawDocument]:
        del ctx
        for req in stream:
            self.seen.append(req)
            yield RawDocument(
                handle=BytesIO(b"binary"),
                source_id=req.source_id,
                metadata=req.metadata,
            )


def test_iter_attachments_skips_filtered_without_http() -> None:
    """Отсеянные attachment'ы не приводят к HTTP-запросу."""
    transport = _FakeTransport()
    flt = AttachmentFilter.from_lists(media_types=["application/pdf"])
    out = list(
        _iter_attachments(
            parent=_parent_with((_ATT_PNG, _ATT_PDF, _ATT_DOCX)),
            base_url="https://confl.example.com/wiki",
            auth=None,
            transport=transport,
            pctx=_pctx(),
            att_filter=flt,
        )
    )
    assert len(out) == 1
    assert out[0].metadata.get(ConfluenceKeys.ATTACHMENT_INFO) == _ATT_PDF
    # PNG и DOCX не должны были даже запроситься
    assert len(transport.seen) == 1
    assert "spec.pdf" in transport.seen[0].url


def test_iter_attachments_passthrough_default_keeps_all() -> None:
    transport = _FakeTransport()
    out = list(
        _iter_attachments(
            parent=_parent_with((_ATT_PNG, _ATT_PDF)),
            base_url="https://confl.example.com/wiki",
            auth=None,
            transport=transport,
            pctx=_pctx(),
        )
    )
    assert len(out) == 2
    assert len(transport.seen) == 2


# -------- ConfluenceContentTransport integration --------


_PAGE_JSON: dict[str, object] = {
    "id": "42",
    "title": "Demo Page",
    "body": {"export_view": {"value": "<p>hi</p>"}},
    "children": {
        "attachment": {
            "results": [
                {
                    "id": "att-1",
                    "title": "diagram.png",
                    "extensions": {"mediaType": "image/png", "fileSize": 10},
                    "version": {"number": 1},
                    "_links": {
                        "download": "/download/attachments/42/diagram.png?version=1",
                    },
                },
                {
                    "id": "att-2",
                    "title": "spec.pdf",
                    "extensions": {
                        "mediaType": "application/pdf",
                        "fileSize": 20,
                    },
                    "version": {"number": 1},
                    "_links": {
                        "download": "/download/attachments/42/spec.pdf?version=1",
                    },
                },
            ],
        },
    },
}


class _FakeInner(Transport[HttpRequest]):
    def __init__(self) -> None:
        self.calls: list[HttpRequest] = []

    def name(self) -> str:
        return "FakeInner"

    def stream(
        self,
        ctx: PipelineContext,
        stream: Iterable[HttpRequest],
    ) -> Iterable[RawDocument]:
        del ctx
        for req in stream:
            self.calls.append(req)
            if req.metadata.has(ConfluenceKeys.ATTACHMENT_INFO):
                att = req.metadata.get(ConfluenceKeys.ATTACHMENT_INFO)
                assert att is not None
                yield RawDocument(
                    handle=BytesIO(b"binary"),
                    source_id=req.source_id,
                    metadata=req.metadata.set(
                        TransportKeys.CONTENT_TYPE, att.media_type,
                    ),
                )
            else:
                yield RawDocument(
                    handle=BytesIO(json.dumps(_PAGE_JSON).encode("utf-8")),
                    source_id=req.source_id,
                    metadata=req.metadata.set(
                        TransportKeys.CONTENT_TYPE, "application/json",
                    ),
                )


def _page_request() -> HttpRequest:
    return HttpRequest(
        url="https://confl.example.com/wiki/rest/api/content/42",
        method="GET",
        source_id=SourceId(
            "https://confl.example.com/wiki/pages/viewpage.action?pageId=42"
        ),
        metadata=(
            Metadata.empty()
            .set(ConfluenceKeys.PAGE_ID, "42")
            .set(ConfluenceKeys.HOST, "confl.example.com")
        ),
    )


def test_transport_applies_attachment_filter() -> None:
    """ConfluenceContentTransport отдаёт только matching attachment'ы."""
    inner = _FakeInner()
    transport = ConfluenceContentTransport(
        inner=inner,
        body_format="export_view",
        base_url="https://confl.example.com/wiki",
        auth=None,
        attachment_filter=AttachmentFilter.from_lists(titles=["*.pdf"]),
    )
    out = list(transport.stream(_pctx(), [_page_request()]))
    assert len(out) == 2  # page + только PDF, без PNG
    assert not out[0].metadata.has(ConfluenceKeys.ATTACHMENT_INFO)
    att = out[1].metadata.get(ConfluenceKeys.ATTACHMENT_INFO)
    assert att is not None
    assert att.title == "spec.pdf"
    # inner: 1 page-request + 1 PDF-request, без PNG
    assert len(inner.calls) == 2
