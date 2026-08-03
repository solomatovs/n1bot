"""Payload внутри настоящей песочницы: реальный rootfs, реальный bwrap.

Остальные тесты payload'а гоняют его питоном приложения — там установлено
всё, поэтому они не видят, чего не хватает в rootfs песочницы. Здесь запуск
идёт ровно так, как в проде: bwrap + rootfs из build/artifacts + payload,
смонтированный read-only. Если rootfs собран без нужного модуля, эти тесты
падают — именно так и должно быть, код требует пересборки rootfs.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from boba.chainlit2.agent.tools.chart import ChartToolsConfig, build_chart_tools
from boba.chainlit2.agent.tools.chart.protocol import (
    ValidateFigureAnswer,
    ValidateFigureRequest,
)
from boba.chainlit2.agent.tools.doc.protocol import (
    DocOutlineAnswer,
    DocPagesAnswer,
    DocPagesRequest,
    DocParams,
    DocPathRequest,
    DocSearchAnswer,
    DocSearchRequest,
    DocTextAnswer,
)
from boba.chainlit2.agent.tools.html import (
    ConfluenceSectionsAnswer,
    ConfluenceSectionsRequest,
    HtmlToMarkdownAnswer,
    HtmlToMarkdownRequest,
    PlainTextAnswer,
    PlainTextRequest,
)
from boba.chainlit2.agent.tools.liteparse import (
    ParseBytesAnswer,
    ParseBytesRequest,
    ParseParams,
)
from boba.chainlit2.agent.tools.liteparse.protocol import ParsedPage
from boba.chainlit2.rendering.tool_result import ChartResult
from boba.chainlit2.sandbox import (
    SandboxCaller,
    SandboxEntryConfig,
    SandboxPayloadError,
    SandboxRunner,
)

_REPO = Path(__file__).resolve().parents[4]
_SANDBOX = _REPO / "build" / "artifacts" / "sandbox"
_ROOTFS = _SANDBOX / "rootfs"
_PAYLOAD = Path(__file__).resolve().parents[1] / "payloads" / "parse"
_ENTRY = ("python3", "/opt/payload/main.py")
_TESSDATA = "/usr/share/tessdata"

_ADDRESS_SPACE = 16 * 1024 * 1024 * 1024
"""RLIMIT_AS профиля парсера. Замер VmPeak внутри песочницы: pdfium резервирует
~2.3G независимо от размера документа (и на 1 странице, и на 200), поэтому
лимит задаёт не расход памяти, а запас поверх этого резерва."""

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

needs_sandbox = pytest.mark.skipif(
    shutil.which("bwrap") is None or not (_ROOTFS / "bin" / "sh").exists(),
    reason="нет bwrap или артефактов песочницы (собрать: make deps)",
)
needs_userns = pytest.mark.skipif(
    os.geteuid() == 0, reason="под root user namespace ведёт себя иначе"
)


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _profile(docs_dir: Path | None = None, **kw: Any) -> dict[str, Any]:
    """Окружение собирается из артефактов, как это делает профиль в конфиге."""
    ro_binds: list[str] = [
        f"{_SANDBOX / 'python'}:/opt/python",
        f"{_SANDBOX / 'site'}:/opt/site",
        f"{_SANDBOX / 'data' / 'fastembed'}:/opt/fastembed",
        f"{_SANDBOX / 'data' / 'tessdata'}:/usr/share/tessdata",
        f"{_PAYLOAD}:/opt/payload",
    ]
    if docs_dir is not None:
        ro_binds.append(f"{docs_dir}:/workspace")
    profile: dict[str, Any] = {
        "rootfs": str(_ROOTFS),
        "ro_binds": tuple(ro_binds),
        "rw_binds": (),
        "rw_images": (),
        "image_template": "",
        "launcher": {
            "mount_wait_sec": 10.0,
            "mount_poll_sec": 0.05,
            "shutdown_wait_sec": 5.0,
            "copy_chunk_bytes": 1 << 20,
        },
        "tmpfs": ("/tmp:256M",),  # noqa: S108
        "network": False,
        "env_set": {
            "PATH": "/opt/python/bin:/usr/local/bin:/usr/bin:/bin",
            "PYTHONHOME": "/opt/python",
            "PYTHONPATH": "/opt/site",
            "LD_LIBRARY_PATH": "/opt/python/lib",
            "HOME": "/tmp",  # noqa: S108
            "LANG": "C.UTF-8",
        },
        "timeout_sec": 120,
        "max_memory_bytes": _ADDRESS_SPACE,
        "max_cpu_sec": 120,
        "max_file_size_bytes": 64 * 1024 * 1024,
        "max_open_files": 1024,
        "max_processes": 256,
        "max_output_bytes": 16 * 1024 * 1024,
        "cgroup_base": "",
        "oom_score_adj": 0,
        "cwd": "/tmp",  # noqa: S108
    }
    profile.update(kw)
    return profile


def _caller(docs_dir: Path | None = None, **kw: Any) -> SandboxCaller:
    sandbox = SandboxEntryConfig.model_validate(
        {"profile": _profile(docs_dir, **kw), "override": {}, "entry": list(_ENTRY)}
    )
    return SandboxCaller("payload-test", sandbox.effective(), dict)


def _params() -> ParseParams:
    return ParseParams(
        ocr_enabled=False,
        ocr_language="eng",
        max_pages=0,
        tessdata_path=_TESSDATA,
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


@needs_sandbox
@needs_userns
class TestDocumentsInSandbox:
    """Инструменты doc: файл лежит в песочнице, парсит его liteparse оттуда."""

    def test_read_document(self, docs: Path) -> None:
        request = DocPathRequest(
            op=DocPathRequest.READ,
            path="/workspace/report.pdf",
            params=_doc_params(),
        )
        answer = _caller(docs).call_json(_ENTRY, request, DocTextAnswer)
        assert answer.num_pages == 2
        assert "Alpha page one" in answer.text

    def test_document_outline(self, docs: Path) -> None:
        request = DocPathRequest(
            op=DocPathRequest.OUTLINE,
            path="/workspace/report.pdf",
            params=_doc_params(),
        )
        answer = _caller(docs).call_json(_ENTRY, request, DocOutlineAnswer)
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
        answer = _caller(docs).call_json(_ENTRY, request, DocSearchAnswer)
        assert [row.page for row in answer.rows] == [1, 2]
        assert "Alpha" in answer.rows[0].snippet

    def test_read_pages(self, docs: Path) -> None:
        answer = _caller(docs).call_json(
            _ENTRY,
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
        answer = _caller().call_json(_ENTRY, request, ParseBytesAnswer)
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
            caller.call_json(_ENTRY, request, DocTextAnswer)
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
            _caller(docs).call_json(_ENTRY, request, DocTextAnswer)

    def test_missing_file_is_reported(self, docs: Path) -> None:
        request = DocPathRequest(
            op=DocPathRequest.READ,
            path="/workspace/нет-такого.pdf",
            params=_doc_params(),
        )
        with pytest.raises(SandboxPayloadError, match="exited with code"):
            _caller(docs).call_json(_ENTRY, request, DocTextAnswer)


@needs_sandbox
@needs_userns
class TestPagesInSandbox:
    """Инструменты web/confluence: HTML разбирается внутри песочницы."""

    def test_to_markdown(self) -> None:
        html = "<html><body><h1>Заголовок</h1><p>Абзац</p></body></html>"
        request = HtmlToMarkdownRequest.of(html, "ATX")
        answer = _caller().call_json(_ENTRY, request, HtmlToMarkdownAnswer)
        assert "# Заголовок" in answer.markdown

    def test_plain_text(self) -> None:
        request = PlainTextRequest.of("<p>Текст <b>жирный</b></p>")
        answer = _caller().call_json(_ENTRY, request, PlainTextAnswer)
        assert answer.text == "Текст жирный"

    def test_confluence_sections(self) -> None:
        request = ConfluenceSectionsRequest.of(_CONFLUENCE_HTML, "Страница")
        answer = _caller().call_json(_ENTRY, request, ConfluenceSectionsAnswer)
        assert [s.heading_text for s in answer.sections] == ["Введение", "Детали"]
        assert answer.sections[0].heading_path == "Страница › Введение"
        assert answer.sections[0].anchor == "intro"
        for section in answer.sections:
            assert "служебное" not in section.content


@needs_sandbox
@needs_userns
class TestChartInSandbox:
    """Схему графика проверяет plotly внутри песочницы."""

    _SPEC = (
        '{"data": [{"type": "bar", "x": ["a", "b"], "y": [1, 2]}], '
        '"layout": {"title": {"text": "Продажи"}}}'
    )

    def test_valid_spec_returns_title(self) -> None:
        request = ValidateFigureRequest.of(self._SPEC)
        answer = _caller().call_json(_ENTRY, request, ValidateFigureAnswer)
        assert answer.title == "Продажи"

    def test_title_may_be_a_plain_string(self) -> None:
        spec = '{"data": [], "layout": {"title": "Отчёт"}}'
        answer = _caller().call_json(
            _ENTRY, ValidateFigureRequest.of(spec), ValidateFigureAnswer
        )
        assert answer.title == "Отчёт"

    def test_spec_without_title(self) -> None:
        spec = '{"data": [{"type": "bar", "x": ["a"], "y": [1]}]}'
        answer = _caller().call_json(
            _ENTRY, ValidateFigureRequest.of(spec), ValidateFigureAnswer
        )
        assert answer.title == ""

    def test_broken_json_is_reported(self) -> None:
        with pytest.raises(SandboxPayloadError, match="not valid JSON"):
            _caller().call_json(
                _ENTRY, ValidateFigureRequest.of("{не json"), ValidateFigureAnswer
            )

    def test_unknown_trace_type_is_reported(self) -> None:
        """Схему держит plotly: выдуманный тип графика должен быть отклонён."""
        spec = '{"data": [{"type": "нет-такого-типа", "x": [1], "y": [2]}]}'
        with pytest.raises(SandboxPayloadError, match="invalid Plotly figure spec"):
            _caller().call_json(
                _ENTRY, ValidateFigureRequest.of(spec), ValidateFigureAnswer
            )

    def test_non_object_spec_is_reported(self) -> None:
        with pytest.raises(SandboxPayloadError, match="must be a JSON figure object"):
            _caller().call_json(
                _ENTRY, ValidateFigureRequest.of("[1, 2, 3]"), ValidateFigureAnswer
            )


@needs_sandbox
@needs_userns
class TestChartTool:
    """Инструмент visualize целиком: LLM -> песочница -> ChartResult."""

    @staticmethod
    def _tool():
        cfg = ChartToolsConfig.model_validate(
            {
                "sandbox": {
                    "profile": _profile(),
                    "override": {},
                    "entry": list(_ENTRY),
                }
            }
        )
        return build_chart_tools(cfg, dict)[0]

    def test_chart_result_carries_spec_and_title(self) -> None:
        spec = (
            '{"data": [{"type": "bar", "x": [1, 2], "y": [3, 1]}], '
            '"layout": {"title": "T"}}'
        )
        message = self._tool().invoke(
            {
                "args": {"spec": spec},
                "id": "call-chart",
                "name": "visualize",
                "type": "tool_call",
            }
        )
        assert isinstance(message.artifact, ChartResult)
        assert message.artifact.title == "T"
        assert message.artifact.spec["data"][0]["type"] == "bar"
        assert message.content == "[chart rendered: T]"

    def test_invalid_spec_reaches_the_caller(self) -> None:
        with pytest.raises(SandboxPayloadError, match="invalid Plotly"):
            self._tool().invoke(
                {
                    "args": {"spec": '{"data": 42}'},
                    "id": "call-chart",
                    "name": "visualize",
                    "type": "tool_call",
                }
            )


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
        sandbox = SandboxEntryConfig.model_validate(
            {"profile": _profile(), "override": {}, "entry": list(_ENTRY)}
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
        sandbox = SandboxEntryConfig.model_validate(
            {"profile": _profile(), "override": {}, "entry": list(_ENTRY)}
        )
        runner = SandboxRunner("kb-test", sandbox.effective(), dict)
        outcome = runner.run(f"test -d {self.WEIGHTS} && ls {self.WEIGHTS}", stdin="")
        assert outcome.succeeded, (
            f"нет весов {self.WEIGHTS}: пересобери — make deps"
        )
        assert outcome.result.stdout.strip()
