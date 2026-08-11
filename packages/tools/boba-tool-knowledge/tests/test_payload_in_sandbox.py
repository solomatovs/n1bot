"""Узлы внутри настоящей песочницы: реальный rootfs, реальный bwrap.

Остальные тесты гоняют payload питоном приложения — там установлено всё,
поэтому они не видят, чего не хватает в rootfs песочницы. Здесь запуск идёт
ровно как в проде: bwrap + rootfs из build/src + payload, смонтированный
read-only. Если rootfs собран без нужного модуля, эти тесты падают — именно
так и должно быть, код требует пересборки rootfs.
"""

from __future__ import annotations

import zipfile
from collections.abc import Mapping
from io import BytesIO
from typing import Any, ClassVar

import pytest
from conftest import (
    StageParts,
    StageTestRegistry,
    needs_sandbox,
    needs_userns,
    sandbox_profile,
)
from pydantic import BaseModel, ConfigDict, Field

from boba.sandbox import SandboxPayloadError
from boba.tool.doc.liteparse import LiteParseCaller, SandboxParserConfig
from boba.toolkit.launcher import PayloadFailureError
from boba.toolkit.workflow import (
    EmptyTrailer,
    StageArgsEnricher,
    StageContract,
    StageSpec,
    WorkflowSpec,
)
from boba.toolkit.workflow import StageNode as DocStageNode

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

_PROBE_SCRIPT = """
import os
import sys
from collections.abc import Mapping
from typing import ClassVar

from pydantic import BaseModel, Field

from boba.toolkit.payload import PayloadChannels, PayloadEntry, PayloadError
from boba.toolkit.workflow import EmptyTrailer


class Request(BaseModel):
    op: str = Field(min_length=1)
    module: str = ""
    path: str = ""


class Ops:
    EXPECTED: ClassVar[Mapping[type[Exception], str]] = {}

    REQUESTS: ClassVar[Mapping[str, type[BaseModel]]] = {"probe": Request}

    @classmethod
    async def dispatch(cls, request, channels):
        assert isinstance(request, Request)
        if request.module:
            __import__(request.module)
        if request.path:
            entries = os.listdir(request.path)
            if not entries:
                raise PayloadError("empty_dir", f"{request.path} is empty")
        return EmptyTrailer()


sys.exit(PayloadEntry.main(Ops))
"""
"""Узел-проба: импортирует модуль rootfs и проверяет каталог весов."""


class ProbeRequest(BaseModel):
    """Запрос пробы: что импортировать и какой каталог должен быть непуст."""

    model_config = ConfigDict(extra="forbid")

    op: str = Field(min_length=1)
    module: str = ""
    path: str = ""


class ProbeSettings(BaseModel):
    """Настройка узла-пробы: имя операции payload'а."""

    model_config = ConfigDict(extra="forbid")

    op: str = Field(min_length=1)


def _probe_nodes() -> Mapping[str, StageParts]:
    node = DocStageNode(
        contract=StageContract(
            accepts=frozenset(),
            out=None,
            result=EmptyTrailer,
        ),
        entry=("python3", "-c", _PROBE_SCRIPT),
        request=ProbeRequest,
        enrich=StageArgsEnricher(ProbeSettings(op="probe")),
    )

    return {"probe": node}


def _probe(**args: Any) -> None:
    """Прогон пробы в песочнице; сбой импорта — ошибка запуска стадии."""
    caller = StageTestRegistry.caller(_probe_nodes(), sandbox_profile())

    spec = WorkflowSpec(nodes=[StageSpec(id="probe", tool="probe", args=args)])
    caller.call(spec)


def _parser_config(**kw: Any) -> SandboxParserConfig:
    fields: dict[str, Any] = {
        "ocr_enabled": False,
        "ocr_language": "eng",
        "max_pages": 0,
        "tessdata_path": _TESSDATA,
        "num_workers": 1,
    }
    fields.update(kw)

    return SandboxParserConfig.model_validate(fields)


def _parser(cfg: SandboxParserConfig, **profile: Any) -> LiteParseCaller:
    nodes = LiteParseCaller.stages("doc", cfg)
    launchers = StageTestRegistry.launchers(nodes, sandbox_profile(**profile))

    return LiteParseCaller("doc", cfg, launchers)


@needs_sandbox
@needs_userns
class TestDocumentsInSandbox:
    """Парсер вложений работает на настоящем rootfs, а не на питоне приложения."""

    def test_pdf_pages_are_parsed(self) -> None:
        answer = _parser(_parser_config()).parse_bytes(_PDF, "report.pdf")
        assert answer.num_pages == 2
        assert "Alpha page one" in answer.pages[0].text

    def test_small_address_space_is_reported(self) -> None:
        """Заниженный RLIMIT_AS ломает pdfium — ошибка должна это показать."""
        parser = _parser(_parser_config(), max_memory_bytes=1024 * 1024 * 1024)

        with pytest.raises(SandboxPayloadError) as failure:
            parser.parse_bytes(_PDF, "report.pdf")

        message = str(failure.value)
        assert "pdfium" in message
        assert "RLIMIT_AS" in message, (
            "падение по адресному пространству должно объясняться словами, "
            f"а не паникой rust: {message}"
        )

    def test_ocr_without_tessdata_is_reported(self) -> None:
        """Без моделей OCR liteparse пошёл бы в сеть; сети в песочнице нет."""
        cfg = _parser_config(ocr_enabled=True, tessdata_path="/нет-такого-каталога")

        with pytest.raises(PayloadFailureError, match="каталога моделей") as failure:
            _parser(cfg).parse_bytes(_PDF, "report.pdf")

        assert failure.value.kind == "document_unreadable"
        assert "Traceback" not in str(failure.value)


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
        "<Relationships xmlns="
        '"http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org'
        '/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    DOCUMENT: ClassVar[str] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        "<w:document xmlns:w="
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

    def _read(self, name: str) -> str:
        answer = _parser(_parser_config()).parse_bytes(self._docx(), name)
        return answer.pages[0].text

    def test_ascii_named_docx_is_readable(self) -> None:
        assert "Alpha section one" in self._read(self.ASCII_NAME)

    def test_cyrillic_named_docx_is_readable(self) -> None:
        assert "Alpha section one" in self._read(self.CYRILLIC_NAME)


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
        _probe(module=module)


@needs_sandbox
@needs_userns
class TestEmbedderInSandbox:
    """Веса эмбеддера лежат в самом образе, монтировать их не нужно."""

    WEIGHTS: ClassVar[str] = "/opt/fastembed"

    def test_weights_are_bundled(self) -> None:
        _probe(path=self.WEIGHTS)
