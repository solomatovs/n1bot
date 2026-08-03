"""Payload внутри настоящей песочницы: реальный rootfs, реальный bwrap.

Остальные тесты payload'а гоняют его питоном приложения — там установлено
всё, поэтому они не видят, чего не хватает в rootfs песочницы. Здесь запуск
идёт ровно так, как в проде: bwrap + rootfs из build/artifacts + payload,
смонтированный read-only. Если rootfs собран без нужного модуля, эти тесты
падают — именно так и должно быть, код требует пересборки rootfs.
"""

from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, ClassVar

import pytest
from conftest import (
    needs_sandbox,
    needs_userns,
    sandbox_profile,
)

from boba.tool.doc.liteparse import (
    ParseBytesAnswer,
    ParseBytesRequest,
)
from boba.tool.doc.liteparse.caller import LiteParseCaller
from boba.tool.doc.liteparse.protocol import ParsedPage, ParseParams
from boba.tool.doc.protocol import (
    DocOutlineAnswer,
    DocPagesAnswer,
    DocPagesRequest,
    DocParams,
    DocPathRequest,
    DocSearchAnswer,
    DocSearchRequest,
    DocTextAnswer,
)
from boba.tool.kb.html import (
    ConfluenceSectionsAnswer,
    ConfluenceSectionsRequest,
    HtmlToMarkdownAnswer,
    HtmlToMarkdownRequest,
    PlainTextAnswer,
    PlainTextRequest,
)
from boba.tool.kb.html.caller import HtmlCaller
from boba.toolkit.sandbox import (
    SandboxCaller,
    SandboxPayloadError,
    SandboxRunner,
    SandboxToolConfig,
)

_TESSDATA = "/usr/share/tessdata"

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

_CONFLUENCE_HTML = (
    "<html><body>"
    '<h1 id="intro">Введение</h1><p>Первый абзац.</p>'
    "<h2>Детали</h2><p>Второй абзац.</p>"
    '<ac:structured-macro ac:name="info">служебное</ac:structured-macro>'
    "</body></html>"
)


def _caller(docs_dir=None, **kw) -> SandboxCaller:
    sandbox = SandboxToolConfig.model_validate(
        {"profile": sandbox_profile(docs_dir, **kw), "override": {}}
    )
    return SandboxCaller("payload-test", sandbox.effective(), dict)


def _params() -> ParseParams:
    return ParseParams(
        ocr_enabled=False,
        ocr_language="eng",
        max_pages=0,
        tessdata_path=_TESSDATA,
        num_workers=1,
    )


def _doc_params(**kw: Any) -> DocParams:
    fields: dict[str, Any] = {**_params().model_dump(), "max_text_chars": 200_000}
    fields.update(kw)
    return DocParams.model_validate(fields)


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.pdf").write_bytes(_PDF)
    return workspace



class TestDocumentsInSandbox:
    """Инструменты doc: файл лежит в песочнице, парсит его liteparse оттуда."""

    def test_read_document(self, docs: Path) -> None:
        request = DocPathRequest(
            op=DocPathRequest.READ,
            path="/workspace/report.pdf",
            params=_doc_params(),
        )
        answer = _caller(docs).call_json(LiteParseCaller.ENTRY, request, DocTextAnswer)
        assert answer.num_pages == 2
        assert "Alpha page one" in answer.text

    def test_document_outline(self, docs: Path) -> None:
        request = DocPathRequest(
            op=DocPathRequest.OUTLINE,
            path="/workspace/report.pdf",
            params=_doc_params(),
        )
        answer = _caller(
            docs).call_json(LiteParseCaller.ENTRY,
            request,
            DocOutlineAnswer,
        )
        assert [row.page for row in answer.rows] == [1, 2]
        assert answer.rows[0].chars > 0

    def test_search_document(self, docs: Path) -> None:
        request = DocSearchRequest(
            op=DocSearchRequest.OP,
            path="/workspace/report.pdf",
            query="Alpha",
            context_chars=5,
            max_matches=50,
            params=_doc_params(),
        )
        answer = _caller(
            docs).call_json(LiteParseCaller.ENTRY,
            request,
            DocSearchAnswer,
        )
        assert [row.page for row in answer.rows] == [1, 2]
        assert "Alpha" in answer.rows[0].snippet

    def test_read_pages(self, docs: Path) -> None:
        answer = _caller(docs).call_json(
            LiteParseCaller.ENTRY,
            DocPagesRequest(
                op=DocPagesRequest.OP,
                path="/workspace/report.pdf",
                pages="2",
                params=_doc_params(),
            ),
            DocPagesAnswer,
        )
        assert answer.pages == (2,)
        assert "Beta page two" in answer.text

    def test_parse_bytes(self) -> None:
        """Вложения приезжают содержимым: файловая система не нужна."""
        request = ParseBytesRequest.of(_PDF, "report.pdf", _params())
        answer = _caller().call_json(LiteParseCaller.ENTRY, request, ParseBytesAnswer)
        assert answer.num_pages == 2
        assert isinstance(answer.pages[0], ParsedPage)
        assert "Alpha page one" in answer.pages[0].text

    def test_small_address_space_is_reported(self, docs: Path) -> None:
        """Заниженный RLIMIT_AS ломает pdfium — ошибка должна это показать."""
        request = DocPathRequest(
            op=DocPathRequest.READ,
            path="/workspace/report.pdf",
            params=_doc_params(),
        )
        caller = _caller(docs, max_memory_bytes=1024 * 1024 * 1024)
        with pytest.raises(SandboxPayloadError) as failure:
            caller.call_json(LiteParseCaller.ENTRY, request, DocTextAnswer)
        message = str(failure.value)
        assert "pdfium" in message
        assert "RLIMIT_AS" in message, (
            "падение по адресному пространству должно объясняться словами, "
            f"а не паникой rust: {message}"
        )

    def test_ocr_without_tessdata_is_reported(self, docs: Path) -> None:
        """Без моделей OCR liteparse пошёл бы в сеть; сети в песочнице нет."""
        params = _doc_params(ocr_enabled=True, tessdata_path="/нет-такого-каталога")
        request = DocPathRequest(
            op=DocPathRequest.READ, path="/workspace/report.pdf", params=params
        )
        with pytest.raises(SandboxPayloadError, match="каталога моделей"):
            _caller(docs).call_json(LiteParseCaller.ENTRY, request, DocTextAnswer)

    def test_missing_file_is_reported(self, docs: Path) -> None:
        request = DocPathRequest(
            op=DocPathRequest.READ,
            path="/workspace/нет-такого.pdf",
            params=_doc_params(),
        )
        with pytest.raises(SandboxPayloadError, match="exited with code"):
            _caller(docs).call_json(LiteParseCaller.ENTRY, request, DocTextAnswer)


@needs_sandbox
@needs_userns
class TestOfficeNonAsciiNames:
    """Конвертация office-документов не должна зависеть от алфавита имени:
    содержимое обоих файлов одинаковое, единственная переменная — имя."""

    ASCII_NAME: ClassVar[str] = "user manual_v9.docx"
    CYRILLIC_NAME: ClassVar[str] = "Инструкция пользователя Магазина данных_v9.docx"

    CONTENT_TYPES: ClassVar[str] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType='
        '"application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument'
        '.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    RELS: ClassVar[str] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns='
        '"http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org'
        '/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    DOCUMENT: ClassVar[str] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w='
        '"http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>Alpha section one</w:t></w:r></w:p></w:body>"
        "</w:document>"
    )

    @classmethod
    def _docx(cls) -> bytes:
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("[Content_Types].xml", cls.CONTENT_TYPES)
            archive.writestr("_rels/.rels", cls.RELS)
            archive.writestr("word/document.xml", cls.DOCUMENT)
        return buffer.getvalue()

    @pytest.fixture
    def office_docs(self, tmp_path: Path) -> Path:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        payload = self._docx()
        (workspace / self.ASCII_NAME).write_bytes(payload)
        (workspace / self.CYRILLIC_NAME).write_bytes(payload)
        return workspace

    def _read(self, workspace: Path, name: str) -> DocTextAnswer:
        request = DocPathRequest(
            op=DocPathRequest.READ,
            path=f"/workspace/{name}",
            params=_doc_params(),
        )
        return _caller(workspace).call_json(
            LiteParseCaller.ENTRY, request, DocTextAnswer
        )

    def test_ascii_named_docx_is_readable(self, office_docs: Path) -> None:
        answer = self._read(office_docs, self.ASCII_NAME)
        assert answer.num_pages >= 1
        assert "Alpha section one" in answer.text

    def test_cyrillic_named_docx_is_readable(self, office_docs: Path) -> None:
        answer = self._read(office_docs, self.CYRILLIC_NAME)
        assert answer.num_pages >= 1
        assert "Alpha section one" in answer.text


@needs_sandbox
@needs_userns
class TestPagesInSandbox:
    """Инструменты web/confluence: HTML разбирается внутри песочницы."""

    def test_to_markdown(self) -> None:
        html = "<html><body><h1>Заголовок</h1><p>Абзац</p></body></html>"
        request = HtmlToMarkdownRequest.of(html, "ATX")
        answer = _caller().call_json(HtmlCaller.ENTRY, request, HtmlToMarkdownAnswer)
        assert "# Заголовок" in answer.markdown

    def test_plain_text(self) -> None:
        request = PlainTextRequest.of("<p>Текст <b>жирный</b></p>")
        answer = _caller().call_json(HtmlCaller.ENTRY, request, PlainTextAnswer)
        assert answer.text == "Текст жирный"

    def test_confluence_sections(self) -> None:
        request = ConfluenceSectionsRequest.of(_CONFLUENCE_HTML, "Страница")
        answer = _caller(
            ).call_json(HtmlCaller.ENTRY,
            request,
            ConfluenceSectionsAnswer,
        )
        assert [s.heading_text for s in answer.sections] == ["Введение", "Детали"]
        assert answer.sections[0].heading_path == "Страница › Введение"
        assert answer.sections[0].anchor == "intro"
        for section in answer.sections:
            assert "служебное" not in section.content


@needs_sandbox
@needs_userns
class TestRootfsContents:
    """Rootfs должен нести всё, что payload импортирует."""

    @pytest.mark.parametrize(
        "module",
        [
            "liteparse",
            "markdownify",
            "bs4",
            "lxml",
            "plotly",
            "httpx",
            "psycopg",
            "fastembed",
            "onnxruntime",
        ],
    )
    def test_module_is_installed(self, module: str) -> None:
        sandbox = SandboxToolConfig.model_validate(
            {"profile": sandbox_profile(), "override": {}}
        )
        runner = SandboxRunner("rootfs-test", sandbox.effective(), dict)
        outcome = runner.run(f"python3 -c 'import {module}'", stdin="")
        assert outcome.succeeded, (
            f"в песочнице нет {module}: пересобери — make deps "
            f"(stderr: {outcome.result.stderr.strip()})"
        )


@needs_sandbox
@needs_userns
class TestEmbedderInSandbox:
    """Веса эмбеддера лежат в самом образе, монтировать их не нужно."""

    WEIGHTS: str = "/opt/fastembed"

    def test_weights_are_bundled(self) -> None:
        sandbox = SandboxToolConfig.model_validate(
            {"profile": sandbox_profile(), "override": {}}
        )
        runner = SandboxRunner("kb-test", sandbox.effective(), dict)
        outcome = runner.run(f"test -d {self.WEIGHTS} && ls {self.WEIGHTS}", stdin="")
        assert outcome.succeeded, (
            f"нет весов {self.WEIGHTS}: пересобери — make deps"
        )
        assert outcome.result.stdout.strip()
