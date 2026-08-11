"""Парсинг вложений: контракт узла parse_bytes и ридер индексации."""

from __future__ import annotations

import base64
import subprocess
import sys
from typing import Any

import pydantic
import pytest
from local_stage import LocalStageLauncher

from boba.indexing import (
    ChunkStream,
    IncompatibleContentError,
    Metadata,
    RawDocument,
    ReaderKeys,
    SectionKeys,
    SourceId,
    TransportKeys,
)
from boba.text.document import DocumentMedia
from boba.tool.doc.liteparse import (
    LiteParseCaller,
    ParseBytesArgs,
    ParseParams,
    SandboxLiteParseReader,
    SandboxParserConfig,
)
from boba.toolkit.launcher import LauncherError, ToolLauncher

pytestmark = pytest.mark.anyio

_TOOL = "confluence"


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


# Двухстраничный PDF: стр.1 "Alpha page one", стр.2 "Beta page two Alpha again".
_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R 6 0 R]/Count 2>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 4 0 R\
/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 50>>stream
BT /F1 20 Tf 20 200 Td (Alpha page one) Tj ET
endstream endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
6 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 7 0 R\
/Resources<</Font<</F1 5 0 R>>>>>>endobj
7 0 obj<</Length 60>>stream
BT /F1 20 Tf 20 200 Td (Beta page two Alpha again) Tj ET
endstream endobj
trailer<</Root 1 0 R/Size 8>>
%%EOF"""

_PDF_TYPE = "application/pdf"


class Launchers:
    """Фабрика порта: один локальный исполнитель узла парсера."""

    def __init__(self, cfg: SandboxParserConfig) -> None:
        self.launcher = LocalStageLauncher(dict(LiteParseCaller.stages(_TOOL, cfg)))

    def __call__(self, tool: str, /) -> ToolLauncher:
        return self.launcher


def _config(**kw: Any) -> SandboxParserConfig:
    fields: dict[str, Any] = {"tessdata_path": "/usr/share/tessdata"}
    fields.update(kw)
    return SandboxParserConfig.model_validate(fields)


@pytest.fixture
def launchers() -> Launchers:
    return Launchers(_config(ocr_language="eng"))


@pytest.fixture
def caller(launchers: Launchers) -> LiteParseCaller:
    return LiteParseCaller(_TOOL, _config(ocr_language="eng"), launchers)


def _raw(data: bytes, content_type: str | None) -> RawDocument:
    metadata = Metadata()
    if content_type is not None:
        metadata = metadata.set(TransportKeys.CONTENT_TYPE, content_type)
    return RawDocument(
        source_id=SourceId("https://confluence/attachment/report.pdf"),
        handle=ChunkStream.of(data),
        metadata=metadata,
    )


class TestParseBytesContract:
    """Документ едет в аргументах base64 и парсится настоящим liteparse."""

    def test_pages_come_back(self, caller: LiteParseCaller) -> None:
        answer = caller.parse_bytes(_PDF, "report.pdf")

        assert answer.num_pages == 2
        assert [page.page_num for page in answer.pages] == [1, 2]
        assert "Alpha page one" in answer.pages[0].text

    def test_text_joins_pages(self, caller: LiteParseCaller) -> None:
        answer = caller.parse_bytes(_PDF, "report.pdf")

        assert "Alpha page one" in answer.text
        assert "Beta page two" in answer.text

    def test_args_carry_base64(self) -> None:
        args = ParseBytesArgs.of(_PDF, "report.pdf")

        assert base64.b64decode(args.content_b64) == _PDF

    def test_parser_settings_come_from_config(
        self, caller: LiteParseCaller, launchers: Launchers
    ) -> None:
        """Настройки парсера кладёт обогатитель узла, а не вызывающий."""
        caller.parse_bytes(_PDF, "report.pdf")

        request = launchers.launcher.requests[0]
        assert request["op"] == "parse_bytes"
        assert request["ocr_language"] == "eng"
        assert request["tessdata_path"] == "/usr/share/tessdata"

    def test_node_name_is_per_tool(self) -> None:
        """У каждого инструмента свой узел парсера: свой профиль и настройки."""
        nodes = LiteParseCaller.stages(_TOOL, _config())

        assert list(nodes) == ["confluence_parse_bytes"]

    def test_tessdata_path_is_required(self) -> None:
        """Единственное поле без дефолта: без каталога моделей запроса нет."""
        with pytest.raises(pydantic.ValidationError):
            ParseParams.model_validate({"ocr_enabled": False})

    def test_broken_document_fails(self, caller: LiteParseCaller) -> None:
        with pytest.raises(LauncherError):
            caller.parse_bytes(b"not a real pdf", "broken.pdf")


class TestSandboxLiteParseReader:
    """Ридер индексации: тот же контракт, что у прежнего LiteParseReader."""

    async def test_section_per_page(self, caller: LiteParseCaller) -> None:
        sections = [
            item
            async for item in SandboxLiteParseReader(caller).read(
                _raw(_PDF, _PDF_TYPE),
            )
        ]
        assert [s.order for s in sections] == [1, 2]
        assert "Alpha page one" in sections[0].content

    async def test_metadata_carries_page_and_doc_type(
        self, caller: LiteParseCaller
    ) -> None:
        [first, *_] = [
            item
            async for item in SandboxLiteParseReader(caller).read(
                _raw(_PDF, _PDF_TYPE),
            )
        ]
        assert first.metadata.get(ReaderKeys.DOC_TYPE) == "pdf"
        assert first.metadata.get(SectionKeys.PAGE_NUMBER) == 1

    async def test_content_type_with_charset(self, caller: LiteParseCaller) -> None:
        raw = _raw(_PDF, "Application/PDF; charset=binary")
        assert [item async for item in SandboxLiteParseReader(caller).read(raw)]

    async def test_unsupported_type_rejected(self, caller: LiteParseCaller) -> None:
        stream = SandboxLiteParseReader(caller).read(_raw(_PDF, "image/png"))

        with pytest.raises(IncompatibleContentError):
            [item async for item in stream]

    async def test_missing_type_rejected(self, caller: LiteParseCaller) -> None:
        stream = SandboxLiteParseReader(caller).read(_raw(_PDF, None))

        with pytest.raises(IncompatibleContentError):
            [item async for item in stream]

    async def test_broken_document_isolated(self, caller: LiteParseCaller) -> None:
        """Битое вложение не должно ронять прогон индексации целиком."""
        stream = SandboxLiteParseReader(caller).read(_raw(b"not a pdf", _PDF_TYPE))

        with pytest.raises(IncompatibleContentError):
            [item async for item in stream]

    async def test_empty_document_yields_nothing(self, caller: LiteParseCaller) -> None:
        empty = SandboxLiteParseReader(caller).read(_raw(b"", _PDF_TYPE))
        assert [section async for section in empty] == []

    def test_media_types_match_suffixes(self, caller: LiteParseCaller) -> None:
        reader = SandboxLiteParseReader(caller)
        assert set(reader.media_types) == set(DocumentMedia.SUFFIX_BY_MEDIA_TYPE)

    async def test_filename_suffix_matches_media_type(
        self, caller: LiteParseCaller, launchers: Launchers
    ) -> None:
        """В запрос уезжает имя с расширением, выведенным из content_type."""
        stream = SandboxLiteParseReader(caller).read(_raw(_PDF, _PDF_TYPE))
        [item async for item in stream]

        assert launchers.launcher.requests[0]["filename"] == "document.pdf"


class TestParserStaysInSandbox:
    """liteparse живёт только в rootfs песочницы, не в окружении приложения."""

    def test_app_does_not_import_liteparse(self) -> None:
        code = (
            "import sys\n"
            "import boba.chainlit.infra.plugins\n"
            "assert 'liteparse' not in sys.modules, 'приложение тянет liteparse'\n"
            "print('ok')\n"
        )
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "ok"
