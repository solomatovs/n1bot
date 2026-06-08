"""ConfluenceJsonDecoder: парсинг children.attachment.results[] в metadata.

Герметичный unit-тест (без сети): подсовываем декодеру вручную собранный
JSON в форме, которую возвращает /rest/api/content/{id}?expand=…,
children.attachment.version,children.attachment.extensions, и проверяем
что ConfluenceKeys.ATTACHMENTS заполнен ровно тем, что мы туда положили.
"""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any

from boba.indexing import Metadata, RawDocument, SourceId, TransportKeys
from boba.tool.kb.confluence.models import AttachmentInfo, ConfluenceKeys
from boba.tool.kb.confluence.parsing import ConfluenceJsonDecoder


def _raw(payload: dict[str, Any]) -> RawDocument:
    return RawDocument(
        handle=BytesIO(json.dumps(payload).encode("utf-8")),
        source_id=SourceId("https://example.com/pages/viewpage.action?pageId=42"),
        metadata=Metadata.empty(),
    )


def _decode(payload: dict[str, Any]) -> RawDocument:
    return ConfluenceJsonDecoder().decode(_raw(payload))


def test_attachments_parsed_into_metadata() -> None:
    payload = {
        "id": "42",
        "title": "Page",
        "body": {"export_view": {"value": "<p>hi</p>"}},
        "children": {
            "attachment": {
                "results": [
                    {
                        "id": "att-1",
                        "title": "diagram.png",
                        "extensions": {
                            "mediaType": "image/png",
                            "fileSize": 12345,
                        },
                        "version": {"number": 3},
                        "_links": {
                            "download": "/download/attachments/42/diagram.png?version=3",
                            "webui": "/pages/viewpageattachments.action?pageId=42&preview=diagram.png",
                        },
                    },
                    {
                        "id": "att-2",
                        "title": "spec.pdf",
                        "extensions": {
                            "mediaType": "application/pdf",
                            "fileSize": 98765,
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
    decoded = _decode(payload)
    items = decoded.metadata.get(ConfluenceKeys.ATTACHMENTS)
    assert items == (
        AttachmentInfo(
            id="att-1",
            title="diagram.png",
            media_type="image/png",
            file_size=12345,
            download_path="/download/attachments/42/diagram.png?version=3",
            webui="/pages/viewpageattachments.action?pageId=42&preview=diagram.png",
            version=3,
        ),
        AttachmentInfo(
            id="att-2",
            title="spec.pdf",
            media_type="application/pdf",
            file_size=98765,
            download_path="/download/attachments/42/spec.pdf?version=1",
            webui="",
            version=1,
        ),
    )


def test_attachments_absent_when_no_children_block() -> None:
    """Page без children в JSON — ключ не выставляется (а не пустой tuple)."""
    decoded = _decode({
        "id": "42",
        "title": "Page",
        "body": {"export_view": {"value": "<p>hi</p>"}},
    })
    assert not decoded.metadata.has(ConfluenceKeys.ATTACHMENTS)


def test_decoded_content_type_is_html_not_json() -> None:
    """После JSON->HTML распаковки TransportKeys.CONTENT_TYPE должен быть text/html.

    HttpTransport ставит application/json (Confluence-ответ), но handle
    содержит уже HTML — DispatchReader должен роутить через HTML-Reader,
    а не пытаться найти Reader для JSON.
    """
    raw = RawDocument(
        handle=BytesIO(json.dumps({
            "id": "42",
            "title": "Page",
            "body": {"export_view": {"value": "<p>hi</p>"}},
        }).encode("utf-8")),
        source_id=SourceId("https://x/pages/viewpage.action?pageId=42"),
        metadata=Metadata.empty().set(TransportKeys.CONTENT_TYPE, "application/json"),
    )
    decoded = ConfluenceJsonDecoder().decode(raw)
    assert decoded.metadata.get(TransportKeys.CONTENT_TYPE) == "text/html"


def test_attachments_absent_when_empty_results() -> None:
    """children.attachment.results = [] — ключ тоже не выставляется."""
    decoded = _decode({
        "id": "42",
        "title": "Page",
        "body": {"export_view": {"value": "<p>hi</p>"}},
        "children": {"attachment": {"results": []}},
    })
    assert not decoded.metadata.has(ConfluenceKeys.ATTACHMENTS)


def test_attachment_missing_extensions_and_version_uses_defaults() -> None:
    """Отсутствующие extensions/version — file_size=0, version=1.

    Confluence в редких конфигурациях возвращает attachment без extensions
    (например, на старых serv'ах с обрезанным expand'ом); декодер не должен
    падать, а возвращать частичный объект с дефолтами.
    """
    decoded = _decode({
        "id": "42",
        "title": "Page",
        "body": {"export_view": {"value": ""}},
        "children": {
            "attachment": {
                "results": [
                    {
                        "id": "att-x",
                        "title": "lone.txt",
                        "_links": {"download": "/download/attachments/42/lone.txt"},
                    },
                ],
            },
        },
    })
    items = decoded.metadata.get(ConfluenceKeys.ATTACHMENTS)
    assert items is not None
    assert len(items) == 1
    a = items[0]
    assert a.id == "att-x"
    assert a.title == "lone.txt"
    assert a.media_type == ""
    assert a.file_size == 0
    assert a.version == 1
    assert a.download_path == "/download/attachments/42/lone.txt"


def test_links_base_and_webui_set_source_url() -> None:
    """SOURCE_URL = _links.base + _links.webui (каноничный URL от Confluence)."""
    decoded = _decode({
        "id": "983136",
        "title": "Page",
        "body": {"export_view": {"value": "<p>hi</p>"}},
        "_links": {
            "webui": "/pages/viewpage.action?pageId=983136",
            "base": "https://confl.loshara.com",
            "self": "https://confl.loshara.com/rest/api/content/983136",
        },
    })
    assert decoded.metadata.get(ConfluenceKeys.SOURCE_URL) == (
        "https://confl.loshara.com/pages/viewpage.action?pageId=983136"
    )


def test_source_url_absent_without_links() -> None:
    """Нет _links.webui/base — SOURCE_URL не ставится (без fallback на хардкод)."""
    decoded = _decode({
        "id": "42",
        "title": "Page",
        "body": {"export_view": {"value": "<p>hi</p>"}},
    })
    assert not decoded.metadata.has(ConfluenceKeys.SOURCE_URL)


def test_version_number_indexed() -> None:
    """version.number попадает в metadata (для сверки устаревания индекса)."""
    decoded = _decode({
        "id": "42",
        "title": "Page",
        "body": {"export_view": {"value": "<p>hi</p>"}},
        "version": {"number": 7},
    })
    assert decoded.metadata.get(ConfluenceKeys.VERSION) == 7


def test_attachments_roundtrip_via_metadata_codec() -> None:
    """Encode-decode цикл через Metadata wire-format остаётся идентичным.

    Это страхует, что AttachmentInfo сериализуется в JSON и обратно без
    потерь — важно когда Metadata уезжает в JSONB Postgres'а (.to_wire()).
    """
    decoded = _decode({
        "id": "42",
        "title": "Page",
        "body": {"export_view": {"value": ""}},
        "children": {
            "attachment": {
                "results": [
                    {
                        "id": "att-1",
                        "title": "diagram.png",
                        "extensions": {"mediaType": "image/png", "fileSize": 100},
                        "version": {"number": 2},
                        "_links": {"download": "/d/diagram.png"},
                    },
                ],
            },
        },
    })
    wire: dict[str, str] = dict(decoded.metadata.to_wire())
    assert ConfluenceKeys.ATTACHMENTS.name in wire
    restored = Metadata.empty()
    for k, v in wire.items():
        if k == ConfluenceKeys.ATTACHMENTS.name:
            restored = restored.set(
                ConfluenceKeys.ATTACHMENTS,
                ConfluenceKeys.ATTACHMENTS.decode(v),
            )
    assert restored.get(ConfluenceKeys.ATTACHMENTS) == decoded.metadata.get(
        ConfluenceKeys.ATTACHMENTS,
    )
