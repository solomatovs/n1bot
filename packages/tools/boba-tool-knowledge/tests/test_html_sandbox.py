"""Разбор HTML в песочнице: контракт payload'а и его место в web/confluence."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import BaseModel

from boba.sandbox import (
    SandboxPayload,
    SandboxPayloadError,
    SandboxToolConfig,
)
from boba.tool.kb.html import (
    ConfluenceSection,
    ConfluenceSectionsRequest,
    HtmlCaller,
    HtmlToMarkdownRequest,
    PlainTextRequest,
)
from boba.toolkit.launcher import ChunkSink

_HTML = (
    "<html><body><h1>Заголовок</h1><p>Абзац с <b>жирным</b>.</p>"
    '<a href="https://example.com">ссылка</a></body></html>'
)

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
    "max_output_bytes": 4 * 1024 * 1024,
    "cgroup_base": "",
    "oom_score_adj": 0,
    "cwd": "/tmp",  # noqa: S108
}


PAYLOAD_MODULE = "boba.tool.kb.html.payload"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _config() -> SandboxToolConfig:
    return SandboxToolConfig.model_validate(
        {
            "profile": _PROFILE,
            "override": {},
        }
    )


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

    @property
    def text(self) -> str:
        return "".join(self.chunks)

    def rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for chunk in self.chunks:
            rows.append(json.loads(chunk))
        return rows


@pytest.fixture
def caller(monkeypatch: pytest.MonkeyPatch) -> HtmlCaller:
    return HtmlCaller("web", lambda _tool: _LocalCaller())


class TestPayloadContract:
    """Payload реально конвертирует HTML и отвечает маркерной строкой."""

    @staticmethod
    def _run(request: HtmlToMarkdownRequest) -> str:
        run = _PayloadRun(request.model_dump_json())
        assert run.returncode == 0, run.stderr
        assert run.trailer is not None
        return run.text

    def test_headings_and_emphasis(self) -> None:
        markdown = self._run(HtmlToMarkdownRequest.of(_HTML, "ATX"))
        assert "# Заголовок" in markdown
        assert "**жирным**" in markdown

    def test_links_are_kept(self) -> None:
        markdown = self._run(HtmlToMarkdownRequest.of(_HTML, "ATX"))
        assert "[ссылка](https://example.com)" in markdown

    def test_empty_html_is_allowed(self) -> None:
        assert self._run(HtmlToMarkdownRequest.of("", "ATX")) == ""

    def test_unknown_op_fails(self) -> None:
        request = json.loads(HtmlToMarkdownRequest.of(_HTML, "ATX").model_dump_json())
        request["op"] = "нет-такой-op"
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", PAYLOAD_MODULE],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert "unknown page op" in result.stderr


_CONFLUENCE_HTML = (
    "<html><body>"
    '<h1 id="intro">Введение</h1><p>Первый абзац.</p>'
    "<h2>Детали</h2><p>Второй абзац.</p>"
    '<ac:structured-macro ac:name="info">служебное</ac:structured-macro>'
    "</body></html>"
)


class TestConfluenceSections:
    """Heading-aware нарезка страницы уехала в песочницу целиком."""

    @staticmethod
    def _run(html: str, title: str) -> list[ConfluenceSection]:
        request = ConfluenceSectionsRequest.of(html, title)
        run = _PayloadRun(request.model_dump_json())
        assert run.returncode == 0, run.stderr
        sections: list[ConfluenceSection] = []
        for row in run.rows():
            sections.append(ConfluenceSection.model_validate(row))
        return sections

    def test_section_per_heading(self) -> None:
        sections = self._run(_CONFLUENCE_HTML, "Страница")
        assert [s.heading_text for s in sections] == ["Введение", "Детали"]
        assert [s.heading_level for s in sections] == [1, 2]

    def test_breadcrumb_starts_from_title(self) -> None:
        sections = self._run(_CONFLUENCE_HTML, "Страница")
        assert sections[0].heading_path == "Страница › Введение"
        assert sections[1].heading_path == "Страница › Введение › Детали"

    def test_text_follows_heading(self) -> None:
        sections = self._run(_CONFLUENCE_HTML, "Страница")
        assert sections[0].content == "Введение\n\nПервый абзац."

    def test_macros_are_dropped(self) -> None:
        """Содержимое ac:*/ri: в текст не попадает."""
        for section in self._run(_CONFLUENCE_HTML, "Страница"):
            assert "служебное" not in section.content

    def test_anchor_from_html_id(self) -> None:
        assert self._run(_CONFLUENCE_HTML, "Страница")[0].anchor == "intro"

    def test_anchor_falls_back_to_index(self) -> None:
        assert self._run(_CONFLUENCE_HTML, "Страница")[1].anchor == "idx:2"

    def test_page_without_headings_gives_one_section(self) -> None:
        sections = self._run("<html><body><p>Просто текст</p></body></html>", "Тема")
        assert len(sections) == 1
        assert sections[0].content == "Тема\n\nПросто текст"
        assert sections[0].heading_level == 0
        assert sections[0].anchor == ""

    def test_empty_page_gives_nothing(self) -> None:
        assert self._run("", "Тема") == []


class TestPlainText:
    """Excerpt'ы поиска тоже разбираются в песочнице."""

    @staticmethod
    def _run(html: str) -> str:
        run = _PayloadRun(PlainTextRequest.of(html).model_dump_json())
        assert run.returncode == 0, run.stderr
        return run.text

    def test_tags_are_stripped(self) -> None:
        assert self._run("<p>Текст <b>жирный</b></p>") == "Текст жирный"

    def test_macros_are_dropped(self) -> None:
        html = (
            '<p>Видно</p><ac:structured-macro ac:name="x">скрыто</ac:structured-macro>'
        )
        assert self._run(html) == "Видно"


class TestHtmlCaller:
    def test_markdown_comes_back(self, caller: HtmlCaller) -> None:
        assert "# Заголовок" in caller.to_markdown(_HTML)

    def test_heading_style_is_explicit(self, caller: HtmlCaller) -> None:
        caller.to_markdown(_HTML)
        sandbox: Any = caller._caller
        assert sandbox.requests[0]["heading_style"] == "ATX"
        assert sandbox.requests[0]["op"] == "to_markdown"


class TestParsersStayInSandbox:
    """Ни один разбор недоверенного ввода не живёт в процессе приложения."""

    @pytest.mark.parametrize(
        "module", ["liteparse", "markdownify", "bs4", "lxml", "plotly"]
    )
    def test_app_does_not_import(self, module: str) -> None:
        code = (
            "import sys\n"
            "import boba.chainlit.infra.plugins\n"
            f"assert {module!r} not in sys.modules, 'приложение тянет {module}'\n"
            "print('ok')\n"
        )
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "ok"
