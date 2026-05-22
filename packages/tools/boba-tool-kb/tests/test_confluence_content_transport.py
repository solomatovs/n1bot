"""`ConfluenceContentTransport`: page → JSON-decode + attachment fan-out.

Тест с фейк-инне́р-транспортом — отдаёт заготовленные JSON-страницы и
бинарные attachment-ответы по флагу `ConfluenceKeys.ATTACHMENT_INFO` в
metadata запроса. Проверяет:

- yield-порядок: сначала page (с `text/html` и `ATTACHMENTS` в meta),
  потом attachments в порядке из JSON;
- attachment-request (предварительно помеченный) проходит через transport
  без декодирования (бинарь остаётся бинарём);
- lazy streaming: pull одного attachment'а не инициирует HTTP остальных;
- если page не имеет вложений — yield'ится только сама page.
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
from boba.tool.kb.confluence._pipeline_common import ConfluenceContentTransport
from boba.tool.kb.confluence.attachments import AttachmentInfo
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.transport.http import HttpRequest


def _pctx() -> PipelineContext:
    return PipelineContext(pipeline_id=PipelineId("test"))


_PAGE_JSON = {
    "id": "42",
    "title": "Demo Page",
    "body": {"export_view": {"value": "<p>hi</p>"}},
    "children": {
        "attachment": {
            "results": [
                {
                    "id": "att-1",
                    "title": "diagram.png",
                    "extensions": {"mediaType": "image/png", "fileSize": 100},
                    "version": {"number": 1},
                    "_links": {
                        "download": "/download/attachments/42/diagram.png?version=1",
                    },
                },
                {
                    "id": "att-2",
                    "title": "spec.pdf",
                    "extensions": {"mediaType": "application/pdf", "fileSize": 200},
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
    """По типу request'а отдаёт либо JSON-page, либо binary-attachment-payload.

    Каждый вызов протоколируется в `self.calls` — для проверки lazy-streaming.
    """

    def __init__(self, *, page_json: dict[str, object]) -> None:
        self._page_json = page_json
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
                yield self._fake_attachment(req)
            else:
                yield self._fake_page(req)

    def _fake_page(self, req: HttpRequest) -> RawDocument:
        return RawDocument(
            handle=BytesIO(json.dumps(self._page_json).encode("utf-8")),
            source_id=req.source_id,
            metadata=req.metadata.set(TransportKeys.CONTENT_TYPE, "application/json"),
        )

    def _fake_attachment(self, req: HttpRequest) -> RawDocument:
        att = req.metadata.get(ConfluenceKeys.ATTACHMENT_INFO)
        assert att is not None
        return RawDocument(
            handle=BytesIO(b"binary:" + att.id.encode()),
            source_id=req.source_id,
            metadata=req.metadata.set(TransportKeys.CONTENT_TYPE, att.media_type),
        )


def _page_request() -> HttpRequest:
    return HttpRequest(
        url="https://confl.example.com/wiki/rest/api/content/42?expand=...",
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


def _new_transport(inner: _FakeInner) -> ConfluenceContentTransport:
    return ConfluenceContentTransport(
        inner=inner,
        body_format="export_view",
        base_url="https://confl.example.com/wiki",
        auth=None,
    )


def test_page_yields_with_html_content_type() -> None:
    """Первый yield — декодированная page с `text/html` (не application/json)."""
    inner = _FakeInner(page_json=_PAGE_JSON)
    transport = _new_transport(inner)
    out = list(transport.stream(_pctx(), [_page_request()]))
    assert out[0].metadata.get(TransportKeys.CONTENT_TYPE) == "text/html"
    assert out[0].handle.read() == b"<p>hi</p>"


def test_yield_order_page_then_attachments() -> None:
    """Yield-порядок: page → att-1 → att-2."""
    inner = _FakeInner(page_json=_PAGE_JSON)
    transport = _new_transport(inner)
    out = list(transport.stream(_pctx(), [_page_request()]))
    assert len(out) == 3
    # page — нет ATTACHMENT_INFO
    assert not out[0].metadata.has(ConfluenceKeys.ATTACHMENT_INFO)
    # attachments — есть ATTACHMENT_INFO, в правильном порядке
    att1 = out[1].metadata.get(ConfluenceKeys.ATTACHMENT_INFO)
    att2 = out[2].metadata.get(ConfluenceKeys.ATTACHMENT_INFO)
    assert att1 is not None and att1.id == "att-1"
    assert att2 is not None and att2.id == "att-2"
    # бинарные content-type из inner-ответа
    assert out[1].metadata.get(TransportKeys.CONTENT_TYPE) == "image/png"
    assert out[2].metadata.get(TransportKeys.CONTENT_TYPE) == "application/pdf"


def test_page_without_attachments_yields_only_page() -> None:
    page_json = {
        "id": "99",
        "title": "Lonely Page",
        "body": {"export_view": {"value": "<p>x</p>"}},
    }
    inner = _FakeInner(page_json=page_json)
    transport = _new_transport(inner)
    out = list(transport.stream(_pctx(), [_page_request()]))
    assert len(out) == 1
    assert out[0].metadata.get(TransportKeys.CONTENT_TYPE) == "text/html"


def test_attachment_request_is_passthrough_not_decoded() -> None:
    """Pre-marked attachment request → inner.stream напрямую, без JSON-парсинга.

    Сценарий: кто-то снаружи (например, retry-логика) подал HttpRequest
    уже с `ATTACHMENT_INFO` в meta. Transport не должен пытаться его
    декодировать как JSON-страницу.
    """
    inner = _FakeInner(page_json=_PAGE_JSON)
    transport = _new_transport(inner)
    att = AttachmentInfo(
        id="att-X",
        title="x.bin",
        media_type="application/octet-stream",
        file_size=10,
        download_path="/download/attachments/42/x.bin",
        version=1,
    )
    req = HttpRequest(
        url="https://confl.example.com/wiki/download/attachments/42/x.bin",
        source_id=SourceId(
            "https://confl.example.com/wiki/download/attachments/42/x.bin"
        ),
        metadata=Metadata.empty().set(ConfluenceKeys.ATTACHMENT_INFO, att),
    )
    out = list(transport.stream(_pctx(), [req]))
    assert len(out) == 1
    # Inner получил ровно один запрос — переданный, не декодированный
    assert len(inner.calls) == 1
    assert inner.calls[0] is req
    # ATTACHMENT_INFO в результате — тот же
    assert out[0].metadata.get(ConfluenceKeys.ATTACHMENT_INFO) == att


def test_lazy_streaming_does_not_prefetch_all_attachments() -> None:
    """Pull page → inner вызван 1 раз. Pull att-1 → 2 раза. Pull att-2 → 3 раза.

    Страхует streaming-контракт: для 50+ attachment'ов consumer не должен
    видеть 51 HTTP-запрос разом.
    """
    inner = _FakeInner(page_json=_PAGE_JSON)
    transport = _new_transport(inner)
    gen = iter(transport.stream(_pctx(), [_page_request()]))
    _page = next(gen)
    assert len(inner.calls) == 1
    _att1 = next(gen)
    assert len(inner.calls) == 2
    _att2 = next(gen)
    assert len(inner.calls) == 3
