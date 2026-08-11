"""Разбор HTML в песочнице: контракт узла и его место в web/confluence.

Payload гоняется на настоящих каналах отдельным процессом: разметка едет в
tool_stdin, продукт — из tool_payload, квитанция — из tool_result.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from typing import Any

import pytest
from conftest import StageTestRegistry, needs_sandbox, needs_userns, sandbox_profile
from pydantic import BaseModel, ConfigDict

from boba.tool.kb.html import (
    ConfluenceSection,
    HtmlCaller,
    HtmlNode,
    HtmlStages,
)
from boba.toolkit.channels import Channel

_HTML = (
    "<html><body><h1>Заголовок</h1><p>Абзац с <b>жирным</b>.</p>"
    '<a href="https://example.com">ссылка</a></body></html>'
)

_CONFLUENCE_HTML = (
    "<html><body>"
    '<h1 id="intro">Введение</h1><p>Первый абзац.</p>'
    "<h2>Детали</h2><p>Второй абзац.</p>"
    '<ac:structured-macro ac:name="info">служебное</ac:structured-macro>'
    "</body></html>"
)

PAYLOAD_MODULE = "boba.tool.kb.html.payload"


class PayloadRun(BaseModel):
    """Итог прогона узла: код возврата, конверт квитанции, продукт, stderr."""

    model_config = ConfigDict(frozen=True)

    code: int
    envelope: dict[str, Any]
    payload: bytes
    stderr: str

    def text(self) -> str:
        return self.payload.decode("utf-8")

    def rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for line in self.text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def sections(self) -> list[ConfluenceSection]:
        sections: list[ConfluenceSection] = []
        for row in self.rows():
            sections.append(ConfluenceSection.model_validate(row))
        return sections


def _read_all(fd: int) -> bytes:
    data = bytearray()
    while True:
        piece = os.read(fd, 65536)
        if not piece:
            break
        data.extend(piece)
    os.close(fd)
    return bytes(data)


def _run(request: Mapping[str, Any], html: str) -> PayloadRun:
    """Прогон payload'а отдельным процессом на реальных pipe-каналах."""
    args_r, args_w = os.pipe()
    stdin_r, stdin_w = os.pipe()
    result_r, result_w = os.pipe()
    payload_r, payload_w = os.pipe()

    env = dict(os.environ)
    channels = {
        Channel.TOOL_ARGS: str(args_r),
        Channel.TOOL_STDIN: str(stdin_r),
        Channel.TOOL_RESULT: str(result_w),
        Channel.TOOL_PAYLOAD: str(payload_w),
        Channel.TOOL_STDOUT: "1",
        Channel.TOOL_STDERR: "2",
    }
    for channel, value in channels.items():
        env[channel.env_name] = value

    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", PAYLOAD_MODULE],
        pass_fds=(args_r, stdin_r, result_w, payload_w),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    for fd in (args_r, stdin_r, result_w, payload_w):
        os.close(fd)

    os.write(args_w, json.dumps(request, ensure_ascii=False).encode("utf-8"))
    os.close(args_w)
    os.write(stdin_w, html.encode("utf-8"))
    os.close(stdin_w)

    _, stderr = proc.communicate(timeout=60)

    result_raw = _read_all(result_r).decode("utf-8").strip()
    payload = _read_all(payload_r)

    envelope: dict[str, Any] = {}
    if result_raw:
        envelope = json.loads(result_raw)

    return PayloadRun(
        code=proc.returncode,
        envelope=envelope,
        payload=payload,
        stderr=stderr.decode("utf-8", errors="replace"),
    )


def _ok(node: HtmlNode, html: str, **args: Any) -> PayloadRun:
    run = _run({"op": node.value, **args}, html)
    assert run.code == 0, run.stderr
    assert "error" not in run.envelope
    return run


class TestPayloadContract:
    """Payload реально конвертирует HTML и отчитывается конвертом квитанции."""

    def test_headings_and_emphasis(self) -> None:
        markdown = _ok(HtmlNode.MARKDOWN, _HTML).text()
        assert "# Заголовок" in markdown
        assert "**жирным**" in markdown

    def test_links_are_kept(self) -> None:
        markdown = _ok(HtmlNode.MARKDOWN, _HTML).text()
        assert "[ссылка](https://example.com)" in markdown

    def test_empty_html_is_allowed(self) -> None:
        assert _ok(HtmlNode.MARKDOWN, "").text() == ""

    def test_bytes_out_counts_the_product(self) -> None:
        run = _ok(HtmlNode.MARKDOWN, _HTML)
        assert run.envelope["bytes_out"] == len(run.payload)

    def test_unknown_op_is_an_invalid_request(self) -> None:
        run = _run({"op": "нет-такой-op"}, _HTML)
        assert run.code != 0
        error = run.envelope["error"]
        assert error["kind"] == "invalid_request"
        assert "unknown op" in error["message"]

    def test_missing_title_is_reported_by_field(self) -> None:
        """Сводка ошибки называет поле, но не печатает содержимое запроса."""
        run = _run({"op": HtmlNode.SECTIONS.value}, _HTML)
        assert run.code != 0
        assert "title" in run.envelope["error"]["message"]


class TestConfluenceSections:
    """Heading-aware нарезка страницы уехала в песочницу целиком."""

    @staticmethod
    def _sections(html: str, title: str) -> list[ConfluenceSection]:
        return _ok(HtmlNode.SECTIONS, html, title=title).sections()

    def test_section_per_heading(self) -> None:
        sections = self._sections(_CONFLUENCE_HTML, "Страница")
        assert [s.heading_text for s in sections] == ["Введение", "Детали"]
        assert [s.heading_level for s in sections] == [1, 2]

    def test_breadcrumb_starts_from_title(self) -> None:
        sections = self._sections(_CONFLUENCE_HTML, "Страница")
        assert sections[0].heading_path == "Страница › Введение"
        assert sections[1].heading_path == "Страница › Введение › Детали"

    def test_text_follows_heading(self) -> None:
        sections = self._sections(_CONFLUENCE_HTML, "Страница")
        assert sections[0].content == "Введение\n\nПервый абзац."

    def test_macros_are_dropped(self) -> None:
        """Содержимое ac:*/ri: в текст не попадает."""
        for section in self._sections(_CONFLUENCE_HTML, "Страница"):
            assert "служебное" not in section.content

    def test_anchor_from_html_id(self) -> None:
        assert self._sections(_CONFLUENCE_HTML, "Страница")[0].anchor == "intro"

    def test_anchor_falls_back_to_index(self) -> None:
        assert self._sections(_CONFLUENCE_HTML, "Страница")[1].anchor == "idx:2"

    def test_page_without_headings_gives_one_section(self) -> None:
        html = "<html><body><p>Просто текст</p></body></html>"
        sections = self._sections(html, "Тема")
        assert len(sections) == 1
        assert sections[0].content == "Тема\n\nПросто текст"
        assert sections[0].heading_level == 0
        assert sections[0].anchor == ""

    def test_empty_page_gives_nothing(self) -> None:
        assert self._sections("", "Тема") == []


class TestPlainText:
    """Excerpt'ы поиска тоже разбираются в песочнице."""

    def test_tags_are_stripped(self) -> None:
        run = _ok(HtmlNode.PLAIN_TEXT, "<p>Текст <b>жирный</b></p>")
        assert run.text() == "Текст жирный"

    def test_macros_are_dropped(self) -> None:
        html = (
            '<p>Видно</p><ac:structured-macro ac:name="x">скрыто</ac:structured-macro>'
        )
        assert _ok(HtmlNode.PLAIN_TEXT, html).text() == "Видно"


@needs_sandbox
@needs_userns
class TestHtmlCallerInSandbox:
    """Фасад собирает граф из одного узла и читает его продукт приёмником."""

    @pytest.fixture
    def caller(self) -> HtmlCaller:
        launchers = StageTestRegistry.launchers(HtmlStages.of(), sandbox_profile())
        return HtmlCaller("confluence", launchers)

    def test_markdown_comes_back(self, caller: HtmlCaller) -> None:
        assert "# Заголовок" in caller.to_markdown(_HTML)

    def test_plain_text_comes_back(self, caller: HtmlCaller) -> None:
        assert caller.plain_text("<p>Текст <b>жирный</b></p>") == "Текст жирный"

    def test_sections_come_back(self, caller: HtmlCaller) -> None:
        answer = caller.confluence_sections(_CONFLUENCE_HTML, "Страница")
        assert [s.heading_text for s in answer.sections] == ["Введение", "Детали"]
        assert answer.sections[0].heading_path == "Страница › Введение"


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
