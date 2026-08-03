"""Разбор HTML в песочнице: контракт payload'а и его место в web/confluence."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest
from pydantic import BaseModel

from boba.tool.kb.html import (
    ConfluenceSectionsAnswer,
    ConfluenceSectionsRequest,
    HtmlCaller,
    HtmlToMarkdownAnswer,
    HtmlToMarkdownRequest,
    PlainTextAnswer,
    PlainTextRequest,
)
from boba.toolkit.sandbox import (
    SandboxPayload,
    SandboxPayloadError,
    SandboxToolConfig,
)

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
def caller(monkeypatch: pytest.MonkeyPatch) -> HtmlCaller:
    from boba.tool.kb.html import caller as caller_module

    monkeypatch.setattr(
        caller_module, "SandboxCaller", lambda *_a, **_kw: _LocalCaller()
    )
    return HtmlCaller("web", _config(), dict)


class TestPayloadContract:
    """Payload реально конвертирует HTML и отвечает маркерной строкой."""

    @staticmethod
    def _run(request: HtmlToMarkdownRequest) -> HtmlToMarkdownAnswer:
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
        return HtmlToMarkdownAnswer.model_validate(
            json.loads(line[len(SandboxPayload.MARKER) :])
        )

    def test_headings_and_emphasis(self) -> None:
        answer = self._run(HtmlToMarkdownRequest.of(_HTML, "ATX"))
        assert "# Заголовок" in answer.markdown
        assert "**жирным**" in answer.markdown

    def test_links_are_kept(self) -> None:
        answer = self._run(HtmlToMarkdownRequest.of(_HTML, "ATX"))
        assert "[ссылка](https://example.com)" in answer.markdown

    def test_empty_html_is_allowed(self) -> None:
        assert self._run(HtmlToMarkdownRequest.of("", "ATX")).markdown == ""

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
    "<ac:structured-macro ac:name=\"info\">служебное</ac:structured-macro>"
    "</body></html>"
)


class TestConfluenceSections:
    """Heading-aware нарезка страницы уехала в песочницу целиком."""

    @staticmethod
    def _run(html: str, title: str) -> ConfluenceSectionsAnswer:
        request = ConfluenceSectionsRequest.of(html, title)
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", PAYLOAD_MODULE],
            input=request.model_dump_json(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        line = result.stdout.splitlines()[-1]
        return ConfluenceSectionsAnswer.model_validate(
            json.loads(line[len(SandboxPayload.MARKER) :])
        )

    def test_section_per_heading(self) -> None:
        answer = self._run(_CONFLUENCE_HTML, "Страница")
        assert [s.heading_text for s in answer.sections] == ["Введение", "Детали"]
        assert [s.heading_level for s in answer.sections] == [1, 2]

    def test_breadcrumb_starts_from_title(self) -> None:
        answer = self._run(_CONFLUENCE_HTML, "Страница")
        assert answer.sections[0].heading_path == "Страница › Введение"
        assert answer.sections[1].heading_path == "Страница › Введение › Детали"

    def test_text_follows_heading(self) -> None:
        answer = self._run(_CONFLUENCE_HTML, "Страница")
        assert answer.sections[0].content == "Введение\n\nПервый абзац."

    def test_macros_are_dropped(self) -> None:
        """Содержимое ac:*/ri: в текст не попадает."""
        answer = self._run(_CONFLUENCE_HTML, "Страница")
        for section in answer.sections:
            assert "служебное" not in section.content

    def test_anchor_from_html_id(self) -> None:
        assert self._run(_CONFLUENCE_HTML, "Страница").sections[0].anchor == "intro"

    def test_anchor_falls_back_to_index(self) -> None:
        assert self._run(_CONFLUENCE_HTML, "Страница").sections[1].anchor == "idx:2"

    def test_page_without_headings_gives_one_section(self) -> None:
        answer = self._run("<html><body><p>Просто текст</p></body></html>", "Тема")
        assert len(answer.sections) == 1
        assert answer.sections[0].content == "Тема\n\nПросто текст"
        assert answer.sections[0].heading_level == 0
        assert answer.sections[0].anchor == ""

    def test_empty_page_gives_nothing(self) -> None:
        assert self._run("", "Тема").sections == ()


class TestPlainText:
    """Excerpt'ы поиска тоже разбираются в песочнице."""

    @staticmethod
    def _run(html: str) -> PlainTextAnswer:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", PAYLOAD_MODULE],
            input=PlainTextRequest.of(html).model_dump_json(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        line = result.stdout.splitlines()[-1]
        return PlainTextAnswer.model_validate(
            json.loads(line[len(SandboxPayload.MARKER) :])
        )

    def test_tags_are_stripped(self) -> None:
        assert self._run("<p>Текст <b>жирный</b></p>").text == "Текст жирный"

    def test_macros_are_dropped(self) -> None:
        html = (
            "<p>Видно</p>"
            '<ac:structured-macro ac:name="x">скрыто</ac:structured-macro>'
        )
        assert self._run(html).text == "Видно"


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
            "import boba.chainlit2.infra.plugins\n"
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
