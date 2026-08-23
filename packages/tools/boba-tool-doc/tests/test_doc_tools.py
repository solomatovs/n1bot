"""Doc-инструменты: тела зовутся напрямую, документ парсит настоящий liteparse."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from boba.text.document import LiteParseError
from boba.tool.doc.tools import (
    EXPECTED,
    TOOLS,
    DocErrorKind,
    DocOutlineRow,
    DocSearchRow,
    DocToolSection,
)
from boba.toolkit.entry import ToolArgv
from boba.toolkit.result import TableResult, TextResult

pytestmark = pytest.mark.anyio


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


def _body(name: str) -> Any:
    for tool in TOOLS:
        if tool.name != name:
            continue

        if tool.coroutine is None:
            raise AssertionError("tool.coroutine is not None")
        return tool.coroutine

    raise AssertionError(f"нет инструмента {name}")


def _cfg(**kw: Any) -> DocToolSection:
    fields: dict[str, Any] = {"tessdata_path": "/usr/share/tessdata"}
    fields.update(kw)
    return DocToolSection.model_validate(fields)


@pytest.fixture
def pdf(tmp_path: Path) -> str:
    path = tmp_path / "doc.pdf"
    path.write_bytes(_PDF)
    return str(path)


class TestReadDocument:
    async def test_returns_all_pages(self, pdf: str) -> None:
        content, artifact = await _body("read_document")(
            path=pdf, pages="1-2", cfg=_cfg()
        )

        if not (isinstance(artifact, TextResult)):
            raise AssertionError("isinstance(artifact, TextResult)")
        if "Alpha page one" not in content:
            raise AssertionError('"Alpha page one" in content')
        if "Beta page two" not in content:
            raise AssertionError('"Beta page two" in content')
        if artifact.metadata["pages"] != "1,2":
            raise AssertionError('artifact.metadata["pages"] == "1,2"')
        if artifact.metadata["truncated"] != "False":
            raise AssertionError('artifact.metadata["truncated"] == "False"')

    async def test_selects_subset(self, pdf: str) -> None:
        content, artifact = await _body("read_document")(
            path=pdf, pages="2", cfg=_cfg()
        )

        if not (isinstance(artifact, TextResult)):
            raise AssertionError("isinstance(artifact, TextResult)")
        if "Beta page two" not in content:
            raise AssertionError('"Beta page two" in content')
        if "page one" in content:
            raise AssertionError('"page one" not in content')
        if artifact.metadata["pages"] != "2":
            raise AssertionError('artifact.metadata["pages"] == "2"')

    async def test_clips_text_and_marks_for_llm(self, pdf: str) -> None:
        content, artifact = await _body("read_document")(
            path=pdf, pages="1-2", cfg=_cfg(max_text_chars=5)
        )

        if not (isinstance(artifact, TextResult)):
            raise AssertionError("isinstance(artifact, TextResult)")
        if artifact.metadata["truncated"] != "True":
            raise AssertionError('artifact.metadata["truncated"] == "True"')
        if "[truncated to 5 characters]" not in content:
            raise AssertionError('"[truncated to 5 characters]" in content')

    async def test_llm_parser_controls_reach_engine(self, pdf: str) -> None:
        """Настройки вызова перекрывают секцию: без tessdata OCR падает сразу."""
        body = _body("read_document")

        with pytest.raises(LiteParseError):
            await body(
                path=pdf,
                pages="1",
                ocr_enabled=True,
                cfg=_cfg(tessdata_path="/нет-такого-каталога"),
            )


class TestDocumentOutline:
    async def test_row_per_page(self, pdf: str) -> None:
        _content, artifact = await _body("document_outline")(path=pdf, cfg=_cfg())

        if not (isinstance(artifact, TableResult)):
            raise AssertionError("isinstance(artifact, TableResult)")

        rows: list[DocOutlineRow] = []
        for raw in artifact.rows:
            rows.append(DocOutlineRow.model_validate(raw))

        if [row.page for row in rows] != [1, 2]:
            raise AssertionError("[row.page for row in rows] == [1, 2]")
        if rows[0].chars <= 0:
            raise AssertionError("rows[0].chars > 0")

        if artifact.note is None:
            raise AssertionError("artifact.note is not None")
        if "pages 2" not in artifact.note:
            raise AssertionError('"pages 2" in artifact.note')


class TestSearchDocument:
    async def test_returns_coordinates_and_snippet(self, pdf: str) -> None:
        _content, artifact = await _body("search_document")(
            path=pdf, query="Alpha", cfg=_cfg()
        )

        if not (isinstance(artifact, TableResult)):
            raise AssertionError("isinstance(artifact, TableResult)")

        rows: list[DocSearchRow] = []
        for raw in artifact.rows:
            rows.append(DocSearchRow.model_validate(raw))

        if [row.page for row in rows] != [1, 2]:
            raise AssertionError("[row.page for row in rows] == [1, 2]")
        if "Alpha" not in rows[0].snippet:
            raise AssertionError('"Alpha" in rows[0].snippet')
        if rows[0].height <= 0:
            raise AssertionError("rows[0].height > 0")

    async def test_reports_limit(self, pdf: str) -> None:
        _content, artifact = await _body("search_document")(
            path=pdf, query="Alpha", cfg=_cfg(search_max_matches=1)
        )

        if not (isinstance(artifact, TableResult)):
            raise AssertionError("isinstance(artifact, TableResult)")
        if len(artifact.rows) != 1:
            raise AssertionError("len(artifact.rows) == 1")

        if artifact.note is None:
            raise AssertionError("artifact.note is not None")
        if "limit reached" not in artifact.note:
            raise AssertionError('"limit reached" in artifact.note')


class TestExpectedFailures:
    async def test_unsupported_format_raises_declared_error(
        self, tmp_path: Path
    ) -> None:
        doc = tmp_path / "notes.md"
        doc.write_text("# Заметки", encoding="utf-8")

        with pytest.raises(LiteParseError, match=r"\.md"):
            await _body("read_document")(path=str(doc), pages="1", cfg=_cfg())

    def test_parse_error_maps_to_document_unreadable(self) -> None:
        if EXPECTED[LiteParseError] is not DocErrorKind.DOCUMENT_UNREADABLE:
            raise AssertionError("EXPECTED[LiteParseError] is DocErrorKind.DOCUMENT_U…")


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
        if [t.name for t in TOOLS] != list(self._NAMES):
            raise AssertionError("[t.name for t in TOOLS] == list(self._NAMES)")

    def test_read_document_requires_path_and_pages(self) -> None:
        schema = self._schema("read_document")

        if "path" not in schema["required"]:
            raise AssertionError('"path" in schema["required"]')
        if "pages" not in schema["required"]:
            raise AssertionError('"pages" in schema["required"]')

    @pytest.mark.parametrize("name", _NAMES)
    def test_cfg_is_hidden_from_llm(self, name: str) -> None:
        """cfg объявлен injected: обёртка запуска снимает его со схемы для LLM."""
        injected = ToolArgv.injected_fields(self._tool(name).args_schema)
        if "cfg" not in injected:
            raise AssertionError('"cfg" in injected fields of the schema')

    @pytest.mark.parametrize("name", _NAMES)
    def test_ocr_controls_are_optional_with_defaults(self, name: str) -> None:
        schema = self._schema(name)
        props = schema["properties"]

        if props["ocr_enabled"]["default"] is not False:
            raise AssertionError('props["ocr_enabled"]["default"] is False')
        if props["num_workers"]["default"] != 1:
            raise AssertionError('props["num_workers"]["default"] == 1')
        if props["num_workers"]["maximum"] != 4:
            raise AssertionError('props["num_workers"]["maximum"] == 4')
        if props["ocr_language"]["default"] != "rus+eng":
            raise AssertionError('props["ocr_language"]["default"] == "rus+eng"')

        for control in ("ocr_enabled", "num_workers", "ocr_language"):
            if control in schema["required"]:
                raise AssertionError('control not in schema["required"]')
