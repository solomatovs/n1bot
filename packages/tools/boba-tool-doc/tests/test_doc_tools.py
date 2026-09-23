"""Doc-инструменты: тела зовутся напрямую, документ читают настоящие ридеры boba-doc."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from boba.doc.config import OcrUnavailableError
from boba.doc.document import BoxedHit, DocumentError
from boba.stand_core.samples import SamplePdf
from boba.tool.doc.tools import (
    EXPECTED,
    TOOLS,
    DocErrorKind,
    DocToolSection,
)
from boba.toolkit.entry import ToolArgv
from boba.toolkit.result import MarkdownResult, TableResult

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


def _body(name: str) -> Any:
    for tool in TOOLS:
        if tool.name != name:
            continue

        if tool.coroutine is None:
            raise AssertionError("tool.coroutine is not None")
        return tool.coroutine

    raise AssertionError(f"нет инструмента {name}")


def _cfg(**kw: Any) -> DocToolSection:
    fields: dict[str, Any] = {
        "text_encodings": ["utf-8"],
        "ocr": {"provider": "off"},
    }
    fields.update(kw)
    return DocToolSection.model_validate(fields)


@pytest.fixture
def pdf(tmp_path: Path) -> str:
    return str(SamplePdf.written(tmp_path))


class TestReadDocument:
    async def test_returns_all_pages(self, pdf: str) -> None:
        artifact = await _body("read_document")(path=pdf, pages="1-2", cfg=_cfg())

        assert isinstance(artifact, MarkdownResult)
        assert SamplePdf.FIRST_PAGE in artifact.text
        assert SamplePdf.SECOND_PAGE in artifact.text
        assert artifact.metadata["pages"] == "1,2"

    async def test_selects_subset(self, pdf: str) -> None:
        artifact = await _body("read_document")(path=pdf, pages="2", cfg=_cfg())

        assert SamplePdf.SECOND_PAGE in artifact.text
        assert SamplePdf.FIRST_PAGE not in artifact.text
        assert artifact.metadata["pages"] == "2"

    async def test_clips_text_and_marks_for_llm(self, pdf: str) -> None:
        artifact = await _body("read_document")(
            path=pdf, pages="1", cfg=_cfg(max_text_chars=5)
        )

        assert artifact.metadata["truncated"] == "True"
        assert "[truncated to 5 characters]" in artifact.text

    async def test_ocr_request_without_provider_is_declared_failure(
        self, pdf: str
    ) -> None:
        with pytest.raises(OcrUnavailableError, match="provider = 'off'"):
            await _body("read_document")(
                path=pdf, pages="1", ocr_enabled=True, cfg=_cfg()
            )

    async def test_bad_pages_spec_is_document_error(self, pdf: str) -> None:
        with pytest.raises(DocumentError, match="numbers and ranges"):
            await _body("read_document")(path=pdf, pages="a-b", cfg=_cfg())


class TestDocumentOutline:
    async def test_row_per_page(self, pdf: str) -> None:
        artifact = await _body("document_outline")(path=pdf, cfg=_cfg())

        assert isinstance(artifact, TableResult)
        assert artifact.note is not None
        assert "pages 2" in artifact.note
        assert [row["number"] for row in artifact.rows] == [1, 2]
        assert artifact.rows[0]["width"] > 0


class TestSearchDocument:
    async def test_returns_coordinates_and_snippet(self, pdf: str) -> None:
        artifact = await _body("search_document")(
            path=pdf, query=SamplePdf.WORD.lower(), offset=0, limit=50, cfg=_cfg()
        )

        assert isinstance(artifact, TableResult)
        rows = [BoxedHit(**raw) for raw in artifact.rows]
        assert [row.page for row in rows] == [1, 2]
        assert SamplePdf.WORD in rows[0].snippet
        assert rows[0].height > 0

    async def test_window_cuts_and_points_further(self, pdf: str) -> None:
        artifact = await _body("search_document")(
            path=pdf, query=SamplePdf.WORD, offset=0, limit=1, cfg=_cfg()
        )

        assert isinstance(artifact, TableResult)
        assert len(artifact.rows) == 1
        assert artifact.note == "rows 1-1; more rows available, next offset=1"
        assert dict(artifact.metadata) == {"path": pdf, "query": SamplePdf.WORD}

    async def test_second_page_is_the_last(self, pdf: str) -> None:
        artifact = await _body("search_document")(
            path=pdf, query=SamplePdf.WORD, offset=1, limit=1, cfg=_cfg()
        )

        assert isinstance(artifact, TableResult)
        assert [BoxedHit(**raw).page for raw in artifact.rows] == [2]
        assert artifact.note == "rows 2-2; end of result"


class TestExpectedFailures:
    async def test_unknown_format_raises_declared_error(self, tmp_path: Path) -> None:
        doc = tmp_path / "blob.xyz"
        doc.write_bytes(b"\x00\x01\x02 not a document")

        with pytest.raises(DocumentError, match="format not recognized"):
            await _body("read_document")(path=str(doc), pages="1", cfg=_cfg())

    async def test_missing_file_raises_declared_error(self, tmp_path: Path) -> None:
        with pytest.raises(DocumentError, match="cannot open the file"):
            await _body("read_document")(
                path=str(tmp_path / "absent.pdf"), pages="1", cfg=_cfg()
            )

    def test_error_kinds(self) -> None:
        assert EXPECTED[DocumentError] is DocErrorKind.DOCUMENT_UNREADABLE
        assert EXPECTED[OcrUnavailableError] is DocErrorKind.OCR_UNAVAILABLE


class TestSchemas:
    _NAMES = ("read_document", "document_outline", "search_document")

    @staticmethod
    def _tool(name: str) -> Any:
        tools: dict[str, Any] = {t.name: t for t in TOOLS}
        return tools[name]

    @classmethod
    def _schema(cls, name: str) -> dict[str, Any]:
        return cls._tool(name).args_schema.model_json_schema()

    def test_all_tools_registered(self) -> None:
        assert [t.name for t in TOOLS] == list(self._NAMES)

    def test_read_document_requires_path_and_pages(self) -> None:
        schema = self._schema("read_document")

        assert "path" in schema["required"]
        assert "pages" in schema["required"]

    @pytest.mark.parametrize("name", _NAMES)
    def test_cfg_is_hidden_from_llm(self, name: str) -> None:
        """cfg объявлен injected: обёртка запуска снимает его со схемы для LLM."""
        injected = ToolArgv.injected_fields(self._tool(name).args_schema)

        assert "cfg" in injected

    @pytest.mark.parametrize("name", _NAMES)
    def test_ocr_is_the_only_optional_control(self, name: str) -> None:
        schema = self._schema(name)
        props = schema["properties"]

        assert props["ocr_enabled"]["default"] is False
        assert "ocr_enabled" not in schema["required"]
        assert "num_workers" not in props
        assert "ocr_language" not in props
