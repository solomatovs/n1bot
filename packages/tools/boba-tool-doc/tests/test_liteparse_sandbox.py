"""Парсинг вложений в песочнице: контракт parse_bytes и ридер индексации."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

import pydantic
import pytest
from pydantic import BaseModel

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
from boba.sandbox import SandboxPayload, SandboxPayloadError
from boba.text.document import DocumentMedia
from boba.tool.doc.liteparse import (
    LiteParseCaller,
    ParseBytesAnswer,
    ParseBytesRequest,
    ParseParams,
    SandboxLiteParseReader,
    SandboxParserConfig,
)
from boba.tool.doc.liteparse.protocol import ParseBytesTrailer, ParsedPage
from boba.toolkit.launcher import ChunkSink

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

_PROFILE: dict[str, Any] = {
    "rootfs": "",
    "ro_binds": (),
    "rw_binds": (),
    "rw_images": (),
    "image_template": "",
    "launcher": {
        "mount_wait_sec": 10.0,
        "mount_poll_sec": 0.05,
        "shutdown_wait_sec": 5.0,
        "lock_wait_sec": 10.0,
        "copy_chunk_bytes": 1 << 20,
    },
    "tmpfs": ("/tmp:64M",),  # noqa: S108
    "network": False,
    "env_set": {"PATH": "/usr/bin:/bin"},
    "timeout_sec": 30,
    "max_memory_bytes": 512 * 1024 * 1024,
    "max_cpu_sec": 30,
    "max_file_size_bytes": 64 * 1024 * 1024,
    "max_open_files": 1024,
    "max_processes": 256,
    "max_output_bytes": 16 * 1024 * 1024,
    "cgroup_base": "",
    "oom_score_adj": 0,
    "cwd": "/tmp",  # noqa: S108
}

_PDF_TYPE = "application/pdf"


PAYLOAD_MODULE = "boba.tool.doc.payload"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _config(**kw: Any) -> SandboxParserConfig:
    fields: dict[str, Any] = {
        "tessdata_path": "/usr/share/tessdata",
        "sandbox": {
            "profile": _PROFILE,
            "override": {},
        },
    }
    fields.update(kw)
    return SandboxParserConfig.model_validate(fields)


class _LocalCaller:
    """Песочница подменена локальным запуском payload'а: контракт тот же."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def call_text(self, command: str, stdin: str) -> Any:
        raise NotImplementedError("этим инструментам нужен только call_stream")

    def call_stream(
        self,
        entry: Sequence[str],
        request: BaseModel,
        sink: ChunkSink,
        trailer: type[BaseModel],
    ) -> Any:
        body = json.loads(request.model_dump_json())
        self.requests.append(body)
        run = _PayloadRun(json.dumps(body))
        if run.returncode != 0:
            raise SandboxPayloadError(run.stderr)
        for chunk in run.chunks:
            sink.write(chunk)
        if run.trailer is None:
            msg = f"payload не напечатал трейлер: {run.stdout!r}"
            raise SandboxPayloadError(msg)
        return trailer.model_validate(run.trailer)


class _PayloadRun:
    """Локальный запуск payload'а: кадры и трейлер по тому же контракту."""

    def __init__(self, stdin: str) -> None:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", PAYLOAD_MODULE],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
        self.returncode = result.returncode
        self.stderr = result.stderr
        self.stdout = result.stdout
        self.chunks: list[str] = []
        self.trailer: dict[str, Any] | None = None
        for line in result.stdout.splitlines():
            if line.startswith(SandboxPayload.CHUNK_MARKER):
                body = line[len(SandboxPayload.CHUNK_MARKER) :]
                self.chunks.append(json.loads(body))
                continue
            if line.startswith(SandboxPayload.MARKER):
                self.trailer = json.loads(line[len(SandboxPayload.MARKER) :])

    def rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for chunk in self.chunks:
            rows.append(json.loads(chunk))
        return rows


@pytest.fixture
def caller(monkeypatch: pytest.MonkeyPatch) -> LiteParseCaller:
    return LiteParseCaller("confluence", _config(), lambda _tool: _LocalCaller())


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
    """Документ едет в запросе base64 и парсится настоящим liteparse."""

    @staticmethod
    def _run(request: ParseBytesRequest) -> ParseBytesAnswer:
        run = _PayloadRun(request.model_dump_json())
        assert run.returncode == 0, run.stderr
        assert run.trailer is not None
        pages: list[ParsedPage] = []
        for row in run.rows():
            pages.append(ParsedPage.model_validate(row))
        trailer = ParseBytesTrailer.model_validate(run.trailer)
        return ParseBytesAnswer(num_pages=trailer.num_pages, pages=tuple(pages))

    @staticmethod
    def _request(data: bytes, filename: str) -> ParseBytesRequest:
        params = ParseParams(
            ocr_enabled=False,
            ocr_language="eng",
            max_pages=0,
            tessdata_path="/usr/share/tessdata",
            num_workers=1,
        )
        return ParseBytesRequest.of(data, filename, params)

    def test_pages_come_back(self) -> None:
        answer = self._run(self._request(_PDF, "report.pdf"))
        assert answer.num_pages == 2
        assert [page.page_num for page in answer.pages] == [1, 2]
        assert "Alpha page one" in answer.pages[0].text

    def test_text_joins_pages(self) -> None:
        answer = self._run(self._request(_PDF, "report.pdf"))
        assert "Alpha page one" in answer.text
        assert "Beta page two" in answer.text

    def test_request_carries_base64(self) -> None:
        request = self._request(_PDF, "report.pdf")
        assert base64.b64decode(request.content_b64) == _PDF

    def test_tessdata_path_is_required(self) -> None:
        """Единственное поле без дефолта: без каталога моделей запроса нет."""
        with pytest.raises(pydantic.ValidationError):
            ParseParams.model_validate({"ocr_enabled": False})

    def test_broken_document_fails(self) -> None:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", PAYLOAD_MODULE],
            input=self._request(b"not a real pdf", "broken.pdf").model_dump_json(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert result.stderr.strip()


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
        self, caller: LiteParseCaller
    ) -> None:
        """В payload уезжает имя с расширением, выведенным из content_type."""
        stream = SandboxLiteParseReader(caller).read(_raw(_PDF, _PDF_TYPE))
        [item async for item in stream]
        sandbox: Any = caller._caller
        assert sandbox.requests[0]["filename"] == "document.pdf"


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
