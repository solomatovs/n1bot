"""KbDocReader: строгий плоский key: value header + обязательные поля."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from boba.indexing import (
    ChunkStream,
    Metadata,
    RawDocument,
    ReaderKeys,
    SourceId,
)
from boba.kbdoc import KbDocFormatError, KbDocKeys, KbDocReader

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


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
            handle=ChunkStream.of(text.encode("utf-8")),
            source_id=SourceId(source_id),
            metadata=Metadata.empty(),
        )

    return _factory


async def test_valid_header_maps_to_metadata(
    make_raw_doc: Callable[..., RawDocument],
) -> None:
    sections = [item async for item in KbDocReader().read(make_raw_doc(_VALID))]

    if len(sections) != 1:
        raise AssertionError("len(sections) == 1")
    sec = sections[0]
    meta = sec.metadata
    if meta.get(ReaderKeys.PAGE_TITLE) != "Правила именования-v6-20260318_191938":
        raise AssertionError('meta.get(ReaderKeys.PAGE_TITLE) == "Правила именования-…')
    if not (
        meta.get(KbDocKeys.SOURCE_URL)
        == ("https://confl.example.com/pages/viewpage.action?pageId=950276")
    ):
        raise AssertionError('meta.get(KbDocKeys.SOURCE_URL) == ( "https://confl.exam…')
    if meta.get(KbDocKeys.PAGE_ID) != "950276":
        raise AssertionError('meta.get(KbDocKeys.PAGE_ID) == "950276"')
    if meta.get(KbDocKeys.SPACE) != "PAAS":
        raise AssertionError('meta.get(KbDocKeys.SPACE) == "PAAS"')
    if sec.tags != frozenset({"dev", "process"}):
        raise AssertionError('sec.tags == frozenset({"dev", "process"})')
    if not (sec.content.startswith("# Правила именования")):
        raise AssertionError('sec.content.startswith("# Правила именования")')


async def test_optional_version_parsed_as_int(
    make_raw_doc: Callable[..., RawDocument],
) -> None:
    """header `version: 7` -> KbDocKeys.VERSION (int) для сверки устаревания."""
    text = _VALID.replace("space: PAAS\n", "space: PAAS\nversion: 7\n")
    sections = [item async for item in KbDocReader().read(make_raw_doc(text))]
    meta = sections[0].metadata
    if meta.get(KbDocKeys.VERSION) != 7:
        raise AssertionError("meta.get(KbDocKeys.VERSION) == 7")


async def test_version_absent_when_no_header_field(
    make_raw_doc: Callable[..., RawDocument],
) -> None:
    """Без `version:` в header'е ключ VERSION не выставляется (пустая колонка)."""
    sections = [item async for item in KbDocReader().read(make_raw_doc(_VALID))]
    meta = sections[0].metadata
    if meta.has(KbDocKeys.VERSION):
        raise AssertionError("not meta.has(KbDocKeys.VERSION)")


def test_url_value_with_colon_not_truncated(
    make_raw_doc: Callable[..., RawDocument],
) -> None:
    parsed = KbDocReader.parse(_VALID)
    if parsed.source_url is None:
        raise AssertionError("parsed.source_url is not None")
    if not (parsed.source_url.startswith("https://")):
        raise AssertionError('parsed.source_url.startswith("https://")')


@pytest.mark.parametrize("missing_line", ["source:", "title:", "page_id:", "space:"])
async def test_missing_required_field_raises(
    make_raw_doc: Callable[..., RawDocument],
    missing_line: str,
) -> None:
    key = missing_line.rstrip(":")
    text = "\n".join(
        line for line in _VALID.splitlines() if not line.startswith(f"{key}:")
    )
    with pytest.raises(KbDocFormatError) as exc:
        [item async for item in KbDocReader().read(make_raw_doc(text))]
    if key not in exc.value.missing:
        raise AssertionError("key in exc.value.missing")


async def test_no_separator_is_invalid(
    make_raw_doc: Callable[..., RawDocument],
) -> None:
    text = "source: https://x\ntitle: T\npage_id: 1\nspace: S\nbody without separator"
    with pytest.raises(KbDocFormatError):
        [item async for item in KbDocReader().read(make_raw_doc(text))]


async def test_empty_body_is_invalid(make_raw_doc: Callable[..., RawDocument]) -> None:
    text = "source: https://x\ntitle: T\npage_id: 1\nspace: S\n---\n"
    with pytest.raises(KbDocFormatError):
        [item async for item in KbDocReader().read(make_raw_doc(text))]


async def test_optional_fields_absent_ok(
    make_raw_doc: Callable[..., RawDocument],
) -> None:
    text = "source: https://x\ntitle: T\npage_id: 1\nspace: S\n---\nbody\n"
    sections = [item async for item in KbDocReader().read(make_raw_doc(text))]
    if len(sections) != 1:
        raise AssertionError("len(sections) == 1")
    if sections[0].tags != frozenset():
        raise AssertionError("sections[0].tags == frozenset()")


def test_unknown_key_goes_to_custom_prefix() -> None:
    text = (
        "source: https://x\ntitle: T\npage_id: 1\nspace: S\nauthor: ivan\n---\nbody\n"
    )
    parsed = KbDocReader.parse(text)
    if parsed.custom != {"author": "ivan"}:
        raise AssertionError('parsed.custom == {"author": "ivan"}')
