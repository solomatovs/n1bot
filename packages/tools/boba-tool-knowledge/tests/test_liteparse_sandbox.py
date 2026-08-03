"""Парсинг вложений в песочнице: контракт parse_bytes и ридер индексации."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from io import BytesIO
from typing import Any

import pytest
from pydantic import BaseModel

from boba.indexing import (
    IncompatibleContentError,
    Metadata,
    RawDocument,
    ReaderKeys,
    SectionKeys,
    SourceId,
    TransportKeys,
)
from boba.tool.doc.liteparse import (
    LiteParseCaller,
    ParseBytesAnswer,
    ParseBytesRequest,
    ParseParams,
    SandboxLiteParseReader,
    SandboxParserConfig,
)
from boba.toolkit.sandbox import SandboxPayload, SandboxPayloadError

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

    def call_json(
        self,
        entry: tuple[str, ...],
        request: BaseModel,
        schema: type[BaseModel],
    ) -> Any:
        body = json.loads(request.model_dump_json())
        self.requests.append(body)
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", PAYLOAD_MODULE],
            input=json.dumps(body),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SandboxPayloadError(result.stderr)
        for line in result.stdout.splitlines():
            if line.startswith(SandboxPayload.MARKER):
                return schema.model_validate(
                    json.loads(line[len(SandboxPayload.MARKER) :])
                )
        msg = f"payload не напечатал результат: {result.stdout!r}"
        raise SandboxPayloadError(msg)


@pytest.fixture
def caller(monkeypatch: pytest.MonkeyPatch) -> LiteParseCaller:
    from boba.tool.doc.liteparse import caller as caller_module

    monkeypatch.setattr(
        caller_module, "SandboxCaller", lambda *_a, **_kw: _LocalCaller()
    )
    return LiteParseCaller("confluence", _config(), dict)


def _raw(data: bytes, content_type: str | None) -> RawDocument:
    metadata = Metadata()
    if content_type is not None:
        metadata = metadata.set(TransportKeys.CONTENT_TYPE, content_type)
    return RawDocument(
        source_id=SourceId("https://confluence/attachment/report.pdf"),
        handle=BytesIO(data),
        metadata=metadata,
    )


class TestParseBytesContract:
    """Документ едет в запросе base64 и парсится настоящим liteparse."""

    @staticmethod
    def _run(request: ParseBytesRequest) -> ParseBytesAnswer:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", PAYLOAD_MODULE],
            input=request.model_dump_json(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        line = result.stdout.splitlines()[-1]
        assert line.startswith(SandboxPayload.MARKER)
        return ParseBytesAnswer.model_validate(
            json.loads(line[len(SandboxPayload.MARKER) :])
        )

    @staticmethod
    def _request(data: bytes, filename: str) -> ParseBytesRequest:
        params = ParseParams(
            ocr_enabled=False,
            ocr_language="eng",
            max_pages=0,
            tessdata_path="/usr/share/tessdata",
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

    def test_section_per_page(self, caller: LiteParseCaller) -> None:
        sections = list(SandboxLiteParseReader(caller).read(_raw(_PDF, _PDF_TYPE)))
        assert [s.order for s in sections] == [1, 2]
        assert "Alpha page one" in sections[0].content

    def test_metadata_carries_page_and_doc_type(
        self, caller: LiteParseCaller
    ) -> None:
        [first, *_] = list(SandboxLiteParseReader(caller).read(_raw(_PDF, _PDF_TYPE)))
        assert first.metadata.get(ReaderKeys.DOC_TYPE) == "pdf"
        assert first.metadata.get(SectionKeys.PAGE_NUMBER) == 1

    def test_content_type_with_charset(self, caller: LiteParseCaller) -> None:
        raw = _raw(_PDF, "Application/PDF; charset=binary")
        assert list(SandboxLiteParseReader(caller).read(raw))

    def test_unsupported_type_rejected(self, caller: LiteParseCaller) -> None:
        with pytest.raises(IncompatibleContentError):
            list(SandboxLiteParseReader(caller).read(_raw(_PDF, "image/png")))

    def test_missing_type_rejected(self, caller: LiteParseCaller) -> None:
        with pytest.raises(IncompatibleContentError):
            list(SandboxLiteParseReader(caller).read(_raw(_PDF, None)))

    def test_broken_document_isolated(self, caller: LiteParseCaller) -> None:
        """Битое вложение не должно ронять прогон индексации целиком."""
        with pytest.raises(IncompatibleContentError):
            list(SandboxLiteParseReader(caller).read(_raw(b"not a pdf", _PDF_TYPE)))

    def test_empty_document_yields_nothing(self, caller: LiteParseCaller) -> None:
        assert list(SandboxLiteParseReader(caller).read(_raw(b"", _PDF_TYPE))) == []

    def test_media_types_match_suffixes(self, caller: LiteParseCaller) -> None:
        reader = SandboxLiteParseReader(caller)
        assert set(reader.media_types) == set(reader.SUFFIX_BY_MEDIA_TYPE)

    def test_filename_suffix_matches_media_type(
        self, caller: LiteParseCaller
    ) -> None:
        """В payload уезжает имя с расширением, выведенным из content_type."""
        list(SandboxLiteParseReader(caller).read(_raw(_PDF, _PDF_TYPE)))
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
