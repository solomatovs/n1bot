"""Doc-инструменты внутри настоящей песочницы: реальный rootfs, реальный bwrap.

Локальные тесты гоняют тела питоном приложения — там установлено всё, поэтому
они не видят, чего не хватает в rootfs песочницы. Здесь запуск идёт ровно как
в проде: bwrap + rootfs из build/src + модуль инструментов на PYTHONPATH.
Если rootfs собран без нужного модуля, эти тесты падают — именно так и должно
быть, код требует пересборки rootfs.
"""

from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, ClassVar

import pytest
from conftest import needs_sandbox, needs_userns, sandbox_profile

from boba.sandbox import (
    SandboxToolConfig,
)
from boba.sandbox.zygote import ZygotePolicy, ZygoteRegistry, ZygoteToolCaller
from boba.toolkit.launcher import LauncherError, ToolOutcome
from boba.toolkit.protocol import ReplyError, ReplyOk, ToolCommand

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


DOC_MODULE = "boba.tool.doc.tools"

ZYGOTE = ZygotePolicy(
    start_timeout_sec=60.0,
    max_start_attempts=1,
    restart_backoff_sec=0.05,
    healthy_after_sec=0.5,
    stop_wait_sec=5.0,
    call_poll_sec=0.05,
)


def _caller(docs_dir: Path | None = None, **kw: Any) -> ZygoteToolCaller:
    """Зигота под каждый набор путей и лимитов: имя секции — ключ реестра."""
    sandbox = SandboxToolConfig.model_validate(
        {"profile": sandbox_profile(docs_dir, **kw), "override": {}}
    )
    profile = sandbox.profile

    section = f"doc-test-{docs_dir}-{sorted(kw.items())}"
    supervisor = ZygoteRegistry.obtain(section, profile, [DOC_MODULE], ZYGOTE)
    return ZygoteToolCaller(section, supervisor, profile)


def _cfg(**kw: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {"tessdata_path": _TESSDATA}
    fields.update(kw)
    return fields


def _run_doc(
    caller: ZygoteToolCaller, tool: str, flags: dict[str, str], cfg: dict[str, Any]
) -> ToolOutcome:
    argv: list[str] = ["python3", "-m", "boba.tool.doc.tools", tool]
    for flag, value in flags.items():
        argv.append(f"--{flag}")
        argv.append(value)

    stdin = json.dumps({"cfg": cfg}).encode("utf-8")
    return caller.run_tool(ToolCommand(argv=tuple(argv), stdin=stdin))


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.pdf").write_bytes(_PDF)
    return workspace


@needs_sandbox
@needs_userns
class TestDocumentsInSandbox:
    """Инструменты doc: файл лежит в песочнице, парсит его liteparse оттуда."""

    def teardown_method(self) -> None:
        ZygoteRegistry.stop_all()

    def test_read_document(self, docs: Path) -> None:
        outcome = _run_doc(
            _caller(docs),
            "read_document",
            {"path": "/workspace/report.pdf", "pages": "1-2"},
            _cfg(),
        )

        reply = outcome.reply
        if not (isinstance(reply, ReplyOk)):
            raise AssertionError("isinstance(reply, ReplyOk)")
        if "Alpha page one" not in reply.content:
            raise AssertionError('"Alpha page one" in reply.content')
        if "Beta page two" not in reply.content:
            raise AssertionError('"Beta page two" in reply.content')

    def test_read_document_page_subset(self, docs: Path) -> None:
        outcome = _run_doc(
            _caller(docs),
            "read_document",
            {"path": "/workspace/report.pdf", "pages": "2"},
            _cfg(),
        )

        reply = outcome.reply
        if not (isinstance(reply, ReplyOk)):
            raise AssertionError("isinstance(reply, ReplyOk)")
        if "Beta page two" not in reply.content:
            raise AssertionError('"Beta page two" in reply.content')
        if "page one" in reply.content:
            raise AssertionError('"page one" not in reply.content')

    def test_document_outline(self, docs: Path) -> None:
        outcome = _run_doc(
            _caller(docs),
            "document_outline",
            {"path": "/workspace/report.pdf"},
            _cfg(),
        )

        reply = outcome.reply
        if not (isinstance(reply, ReplyOk)):
            raise AssertionError("isinstance(reply, ReplyOk)")
        if "pages 2" not in reply.content:
            raise AssertionError('"pages 2" in reply.content')

    def test_search_document(self, docs: Path) -> None:
        outcome = _run_doc(
            _caller(docs),
            "search_document",
            {"path": "/workspace/report.pdf", "query": "Alpha"},
            _cfg(),
        )

        reply = outcome.reply
        if not (isinstance(reply, ReplyOk)):
            raise AssertionError("isinstance(reply, ReplyOk)")
        if "Alpha" not in reply.content:
            raise AssertionError('"Alpha" in reply.content')

    def test_small_address_space_is_reported(self, docs: Path) -> None:
        """Заниженный RLIMIT_AS ломает pdfium — ошибка должна это объяснить."""
        caller = _caller(docs, process_memory_bytes=512 * 1024 * 1024)

        # конкретный класс задаёт исполнитель; контракт слоя — LauncherError
        with pytest.raises(LauncherError) as failure:
            _run_doc(
                caller,
                "read_document",
                {"path": "/workspace/report.pdf", "pages": "1-2"},
                _cfg(),
            )

        message = str(failure.value)
        if "RLIMIT_AS" not in message:
            raise AssertionError(
                "падение по адресному пространству должно объясняться словами, "
                f"а не паникой rust: {message}"
            )

    def test_ocr_without_tessdata_is_reported(self, docs: Path) -> None:
        """Без моделей OCR liteparse пошёл бы в сеть; сети в песочнице нет."""
        outcome = _run_doc(
            _caller(docs),
            "read_document",
            {"path": "/workspace/report.pdf", "pages": "1-2", "ocr-enabled": "true"},
            _cfg(tessdata_path="/нет-такого-каталога"),
        )

        reply = outcome.reply
        if not (isinstance(reply, ReplyError)):
            raise AssertionError("isinstance(reply, ReplyError)")
        if reply.kind != "document_unreadable":
            raise AssertionError('reply.kind == "document_unreadable"')
        if "Traceback" in reply.message:
            raise AssertionError('"Traceback" not in reply.message')

    def test_missing_file_is_a_declared_failure(self, docs: Path) -> None:
        outcome = _run_doc(
            _caller(docs),
            "read_document",
            {"path": "/workspace/нет-такого.pdf", "pages": "1"},
            _cfg(),
        )

        reply = outcome.reply
        if not (isinstance(reply, ReplyError)):
            raise AssertionError("isinstance(reply, ReplyError)")
        if reply.kind != "document_unreadable":
            raise AssertionError('reply.kind == "document_unreadable"')
        if "Traceback" in reply.message:
            raise AssertionError('"Traceback" not in reply.message')


@needs_sandbox
@needs_userns
class TestOfficeNonAsciiNames:
    """Конвертация office-документов не должна зависеть от алфавита имени:
    содержимое обоих файлов одинаковое, единственная переменная — имя."""

    def teardown_method(self) -> None:
        ZygoteRegistry.stop_all()

    ASCII_NAME: ClassVar[str] = "user manual_v9.docx"
    CYRILLIC_NAME: ClassVar[str] = "Инструкция пользователя Магазина данных_v9.docx"

    FIXTURES: ClassVar[Path] = Path(__file__).parent / "fixtures" / "docx"
    """Части docx лежат файлами: в коде их namespace'ам делать нечего."""

    PARTS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("[Content_Types].xml", "content_types.xml"),
        ("_rels/.rels", "rels.xml"),
        ("word/document.xml", "document.xml"),
    )

    @classmethod
    def _docx(cls) -> bytes:
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for inside, fixture in cls.PARTS:
                body = (cls.FIXTURES / fixture).read_text(encoding="utf-8")
                archive.writestr(inside, body)
        return buffer.getvalue()

    @pytest.fixture
    def office_docs(self, tmp_path: Path) -> Path:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        payload = self._docx()
        (workspace / self.ASCII_NAME).write_bytes(payload)
        (workspace / self.CYRILLIC_NAME).write_bytes(payload)
        return workspace

    def _read(self, workspace: Path, name: str) -> ReplyOk:
        outcome = _run_doc(
            _caller(workspace),
            "read_document",
            {"path": f"/workspace/{name}", "pages": "1"},
            _cfg(),
        )

        reply = outcome.reply
        if not (isinstance(reply, ReplyOk)):
            raise AssertionError(reply)
        return reply

    def test_ascii_named_docx_is_readable(self, office_docs: Path) -> None:
        reply = self._read(office_docs, self.ASCII_NAME)
        if "Alpha section one" not in reply.content:
            raise AssertionError('"Alpha section one" in reply.content')

    def test_cyrillic_named_docx_is_readable(self, office_docs: Path) -> None:
        reply = self._read(office_docs, self.CYRILLIC_NAME)
        if "Alpha section one" not in reply.content:
            raise AssertionError('"Alpha section one" in reply.content')


@needs_sandbox
@needs_userns
class TestRootfsContents:
    """Rootfs должен нести всё, что импортируют тела инструментов."""

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
        sandbox = SandboxToolConfig.model_validate({"profile": sandbox_profile()})
        profile = sandbox.profile
        supervisor = ZygoteRegistry.obtain("rootfs-test", profile, (), ZYGOTE)
        caller = ZygoteToolCaller("rootfs-test", supervisor, profile)
        try:
            outcome = caller.call_text(f"python3 -c 'import {module}'", stdin="")
        finally:
            ZygoteRegistry.stop_all()

        if outcome.result.exit_code != 0:
            raise AssertionError(
                f"в песочнице нет {module}: пересобери — make deps "
                f"(stderr: {outcome.result.stderr.strip()})"
            )


@needs_sandbox
@needs_userns
class TestEmbedderInSandbox:
    """Веса эмбеддера лежат в самом образе, монтировать их не нужно."""

    WEIGHTS: str = "/var/cache/fastembed"

    def test_weights_are_bundled(self) -> None:
        sandbox = SandboxToolConfig.model_validate({"profile": sandbox_profile()})
        profile = sandbox.profile
        supervisor = ZygoteRegistry.obtain("kb-test", profile, (), ZYGOTE)
        caller = ZygoteToolCaller("kb-test", supervisor, profile)
        try:
            outcome = caller.call_text(
                f"test -d {self.WEIGHTS} && ls {self.WEIGHTS}", stdin=""
            )
        finally:
            ZygoteRegistry.stop_all()

        if outcome.result.exit_code != 0:
            raise AssertionError(f"нет весов {self.WEIGHTS}: пересобери — make deps")
        if not (outcome.result.stdout.strip()):
            raise AssertionError("outcome.result.stdout.strip()")
