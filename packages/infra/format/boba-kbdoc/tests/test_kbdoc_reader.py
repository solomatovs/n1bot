"""KbDocReader: строгий плоский `key: value` header + обязательные поля."""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO

import pytest

from boba.indexing import (
    Metadata,
    RawDocument,
    ReaderKeys,
    SourceId,
)
from boba.kbdoc import KbDocFormatError, KbDocKeys, KbDocReader

_VALID = (
    "source: https://confl.example.com/pages/viewpage.action?pageId=950276\n"
    "title: Правила именования-v6-20260318_191938\n"
    "page_id: 950276\n"
    "space: PAAS\n"
    "tags: dev, process\n"
    "---\n"
    "\n"
    "# Правила именования\n"
    "body content\n"
)


@pytest.fixture
def make_raw_doc() -> Callable[..., RawDocument]:
    def _factory(text: str, *, source_id: str = "ws:sess:upload/x.md") -> RawDocument:
        return RawDocument(
            handle=BytesIO(text.encode("utf-8")),
            source_id=SourceId(source_id),
            metadata=Metadata.empty(),
        )

    return _factory


def test_valid_header_maps_to_metadata(
    make_raw_doc: Callable[..., RawDocument],
) -> None:
    sections = list(KbDocReader().convert(make_raw_doc(_VALID)))

    assert len(sections) == 1
    sec = sections[0]
    meta = sec.metadata
    assert meta.get(ReaderKeys.PAGE_TITLE) == "Правила именования-v6-20260318_191938"
    assert meta.get(KbDocKeys.SOURCE_URL) == (
        "https://confl.example.com/pages/viewpage.action?pageId=950276"
    )
    assert meta.get(KbDocKeys.PAGE_ID) == "950276"
    assert meta.get(KbDocKeys.SPACE) == "PAAS"
    assert sec.tags == frozenset({"dev", "process"})
    assert sec.content.startswith("# Правила именования")


def test_url_value_with_colon_not_truncated(
    make_raw_doc: Callable[..., RawDocument],
) -> None:
    parsed = KbDocReader.parse(_VALID)
    assert parsed.source_url is not None
    assert parsed.source_url.startswith("https://")


@pytest.mark.parametrize("missing_line", ["source:", "title:", "page_id:", "space:"])
def test_missing_required_field_raises(
    make_raw_doc: Callable[..., RawDocument],
    missing_line: str,
) -> None:
    key = missing_line.rstrip(":")
    text = "\n".join(
        line for line in _VALID.splitlines() if not line.startswith(f"{key}:")
    )
    with pytest.raises(KbDocFormatError) as exc:
        list(KbDocReader().convert(make_raw_doc(text)))
    assert key in exc.value.missing


def test_no_separator_is_invalid(make_raw_doc: Callable[..., RawDocument]) -> None:
    text = "source: https://x\ntitle: T\npage_id: 1\nspace: S\nbody without separator"
    with pytest.raises(KbDocFormatError):
        list(KbDocReader().convert(make_raw_doc(text)))


def test_empty_body_is_invalid(make_raw_doc: Callable[..., RawDocument]) -> None:
    text = (
        "source: https://x\ntitle: T\npage_id: 1\nspace: S\n---\n"
    )
    with pytest.raises(KbDocFormatError):
        list(KbDocReader().convert(make_raw_doc(text)))


def test_optional_fields_absent_ok(make_raw_doc: Callable[..., RawDocument]) -> None:
    text = (
        "source: https://x\ntitle: T\npage_id: 1\nspace: S\n---\nbody\n"
    )
    sections = list(KbDocReader().convert(make_raw_doc(text)))
    assert len(sections) == 1
    assert sections[0].tags == frozenset()


def test_unknown_key_goes_to_custom_prefix() -> None:
    text = (
        "source: https://x\ntitle: T\npage_id: 1\nspace: S\n"
        "author: ivan\n---\nbody\n"
    )
    parsed = KbDocReader.parse(text)
    assert parsed.custom == {"author": "ivan"}
