"""Fan-out: `make_attachment_request` + `_iter_attachments` без сети.

`make_attachment_request` тестируем чисто (unit). `_iter_attachments` —
с фейк-транспортом, который записывает, какие requests ему пришли, и
возвращает заранее заготовленные RawDocument'ы. Так проверяем yield-
порядок, что transport вызывается ровно по одному разу на attachment,
и что родительская metadata корректно копируется на дочерние.
"""

from __future__ import annotations

from collections.abc import Iterable
from io import BytesIO

from boba.indexing import (
    Metadata,
    PipelineContext,
    PipelineId,
    RawDocument,
    SourceId,
    Transport,
)
from boba.tool.kb.confluence._pipeline_common import _iter_attachments
from boba.tool.kb.confluence.attachments import AttachmentInfo
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.tool.kb.confluence.request_sources._common import make_attachment_request
from boba.transport.http import HttpRequest

_ATT1 = AttachmentInfo(
    id="att-1",
    title="diagram.png",
    media_type="image/png",
    file_size=12345,
    download_path="/download/attachments/42/diagram.png?version=3",
    version=3,
)
_ATT2 = AttachmentInfo(
    id="att-2",
    title="spec.pdf",
    media_type="application/pdf",
    file_size=98765,
    download_path="/download/attachments/42/spec.pdf?version=1",
    version=1,
)


def _parent_meta() -> Metadata:
    return (
        Metadata.empty()
        .set(ConfluenceKeys.PAGE_ID, "42")
        .set(ConfluenceKeys.HOST, "confl.example.com")
        .set(ConfluenceKeys.SPACE_KEY, "DEMO")
        .set(ConfluenceKeys.ANCESTORS_TITLES, ("Root", "Parent"))
    )


def _pctx() -> PipelineContext:
    return PipelineContext(pipeline_id=PipelineId("test"))


class _FakeTransport(Transport[HttpRequest]):
    """Записывает приходящие requests и yield'ит per-request фиктивный RawDocument."""

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
                handle=BytesIO(b"binary-payload-for-" + req.source_id.encode()),
                source_id=req.source_id,
                metadata=req.metadata,
            )


# -------- make_attachment_request --------


def test_make_attachment_request_url_combines_base_and_download_path() -> None:
    req = make_attachment_request(
        base_url="https://confl.example.com/wiki",
        auth=None,
        parent_metadata=_parent_meta(),
        attachment=_ATT1,
    )
    assert req.url == (
        "https://confl.example.com/wiki/download/attachments/42/diagram.png?version=3"
    )
    assert str(req.source_id) == req.url


def test_make_attachment_request_trims_trailing_slash_in_base_url() -> None:
    req = make_attachment_request(
        base_url="https://confl.example.com/wiki/",
        auth=None,
        parent_metadata=_parent_meta(),
        attachment=_ATT1,
    )
    assert "/wiki//download" not in req.url


def test_make_attachment_request_propagates_parent_metadata() -> None:
    req = make_attachment_request(
        base_url="https://confl.example.com/wiki",
        auth=None,
        parent_metadata=_parent_meta(),
        attachment=_ATT1,
    )
    assert req.metadata.get(ConfluenceKeys.PAGE_ID) == "42"
    assert req.metadata.get(ConfluenceKeys.HOST) == "confl.example.com"
    assert req.metadata.get(ConfluenceKeys.SPACE_KEY) == "DEMO"
    assert req.metadata.get(ConfluenceKeys.ANCESTORS_TITLES) == ("Root", "Parent")
    assert req.metadata.get(ConfluenceKeys.ATTACHMENT_INFO) == _ATT1


def test_make_attachment_request_handles_missing_parent_fields() -> None:
    """Если у родителя нет SPACE_KEY/ANCESTORS — на дочернем их тоже нет."""
    parent = Metadata.empty().set(ConfluenceKeys.PAGE_ID, "42")
    req = make_attachment_request(
        base_url="https://confl.example.com/wiki",
        auth=None,
        parent_metadata=parent,
        attachment=_ATT1,
    )
    assert req.metadata.get(ConfluenceKeys.PAGE_ID) == "42"
    assert not req.metadata.has(ConfluenceKeys.SPACE_KEY)
    assert not req.metadata.has(ConfluenceKeys.ANCESTORS_TITLES)
    assert not req.metadata.has(ConfluenceKeys.HOST)


# -------- _iter_attachments --------


def _parent_with(attachments: tuple[AttachmentInfo, ...] | None) -> RawDocument:
    meta = _parent_meta()
    if attachments is not None:
        meta = meta.set(ConfluenceKeys.ATTACHMENTS, attachments)
    return RawDocument(
        handle=BytesIO(b"<html>page</html>"),
        source_id=SourceId(
            "https://confl.example.com/wiki/pages/viewpage.action?pageId=42"
        ),
        metadata=meta,
    )


def test_iter_attachments_yields_nothing_when_key_absent() -> None:
    """Page без `ATTACHMENTS` в метадате — fan-out не делает HTTP-вызовов."""
    transport = _FakeTransport()
    out = list(
        _iter_attachments(
            parent=_parent_with(None),
            base_url="https://confl.example.com/wiki",
            auth=None,
            transport=transport,
            pctx=_pctx(),
        )
    )
    assert out == []
    assert transport.seen == []


def test_iter_attachments_yields_nothing_when_empty_tuple() -> None:
    transport = _FakeTransport()
    out = list(
        _iter_attachments(
            parent=_parent_with(()),
            base_url="https://confl.example.com/wiki",
            auth=None,
            transport=transport,
            pctx=_pctx(),
        )
    )
    assert out == []
    assert transport.seen == []


def test_iter_attachments_streams_in_order() -> None:
    """Yield-порядок совпадает с порядком в `ATTACHMENTS`."""
    transport = _FakeTransport()
    out = list(
        _iter_attachments(
            parent=_parent_with((_ATT1, _ATT2)),
            base_url="https://confl.example.com/wiki",
            auth=None,
            transport=transport,
            pctx=_pctx(),
        )
    )
    assert len(out) == 2
    assert out[0].metadata.get(ConfluenceKeys.ATTACHMENT_INFO) == _ATT1
    assert out[1].metadata.get(ConfluenceKeys.ATTACHMENT_INFO) == _ATT2
    assert [r.url for r in transport.seen] == [
        "https://confl.example.com/wiki/download/attachments/42/diagram.png?version=3",
        "https://confl.example.com/wiki/download/attachments/42/spec.pdf?version=1",
    ]


def test_iter_attachments_does_not_materialize_full_list() -> None:
    """Pull одного attachment'а не должен тащить остальные через transport.

    Страхует streaming-контракт: если consumer прочитал первый attachment
    и остановился, второй HTTP ещё не должен быть инициирован. Это критично,
    если у страницы 50+ вложений.
    """
    transport = _FakeTransport()
    gen = _iter_attachments(
        parent=_parent_with((_ATT1, _ATT2)),
        base_url="https://confl.example.com/wiki",
        auth=None,
        transport=transport,
        pctx=_pctx(),
    )
    first = next(gen)
    assert first.metadata.get(ConfluenceKeys.ATTACHMENT_INFO) == _ATT1
    assert len(transport.seen) == 1  # второй ещё не запрашивали
    second = next(gen)
    assert second.metadata.get(ConfluenceKeys.ATTACHMENT_INFO) == _ATT2
    assert len(transport.seen) == 2


def test_iter_attachments_propagates_parent_keys() -> None:
    transport = _FakeTransport()
    out = list(
        _iter_attachments(
            parent=_parent_with((_ATT1,)),
            base_url="https://confl.example.com/wiki",
            auth=None,
            transport=transport,
            pctx=_pctx(),
        )
    )
    meta = out[0].metadata
    assert meta.get(ConfluenceKeys.PAGE_ID) == "42"
    assert meta.get(ConfluenceKeys.SPACE_KEY) == "DEMO"
    assert meta.get(ConfluenceKeys.ANCESTORS_TITLES) == ("Root", "Parent")
