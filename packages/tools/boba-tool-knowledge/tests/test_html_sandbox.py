"""Разбор Confluence-HTML: PageOps как чистые функции + изоляция парсеров.

PageOps больше не payload за протоколом — его зовут тела инструментов
напрямую, поэтому и тесты зовут его напрямую.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from boba.tool.kb.html.payload import PageOps

_HTML = (
    "<html><body><h1>Заголовок</h1><p>Абзац с <b>жирным</b>.</p>"
    '<a href="https://example.com">ссылка</a></body></html>'
)


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestToMarkdown:
    """Конвертация HTML в Markdown."""

    @staticmethod
    def _run(html: str) -> str:
        answer = PageOps.to_markdown({"html": html, "heading_style": "ATX"})
        return str(answer["markdown"])

    def test_headings_and_emphasis(self) -> None:
        markdown = self._run(_HTML)
        assert "# Заголовок" in markdown
        assert "**жирным**" in markdown

    def test_links_are_kept(self) -> None:
        assert "[ссылка](https://example.com)" in self._run(_HTML)

    def test_empty_html_is_allowed(self) -> None:
        assert self._run("") == ""


_CONFLUENCE_HTML = (
    "<html><body>"
    '<h1 id="intro">Введение</h1><p>Первый абзац.</p>'
    "<h2>Детали</h2><p>Второй абзац.</p>"
    '<ac:structured-macro ac:name="info">служебное</ac:structured-macro>'
    "</body></html>"
)


class TestConfluenceSections:
    """Heading-aware нарезка страницы."""

    @staticmethod
    def _run(html: str, title: str) -> list[dict[str, object]]:
        answer = PageOps.confluence_sections({"html": html, "title": title})
        return list(answer["sections"])

    def test_section_per_heading(self) -> None:
        sections = self._run(_CONFLUENCE_HTML, "Страница")
        assert [s["heading_text"] for s in sections] == ["Введение", "Детали"]
        assert [s["heading_level"] for s in sections] == [1, 2]

    def test_breadcrumb_starts_from_title(self) -> None:
        sections = self._run(_CONFLUENCE_HTML, "Страница")
        assert sections[0]["heading_path"] == "Страница › Введение"
        assert sections[1]["heading_path"] == "Страница › Введение › Детали"

    def test_text_follows_heading(self) -> None:
        sections = self._run(_CONFLUENCE_HTML, "Страница")
        assert sections[0]["content"] == "Введение\n\nПервый абзац."

    def test_macros_are_dropped(self) -> None:
        """Содержимое ac:*/ri: в текст не попадает."""
        for section in self._run(_CONFLUENCE_HTML, "Страница"):
            assert "служебное" not in str(section["content"])


class TestPlainText:
    def test_tags_are_stripped(self) -> None:
        answer = PageOps.plain_text({"html": _HTML})
        text = str(answer["text"])
        assert "Заголовок" in text
        assert "<b>" not in text


class TestParsersStayInSandbox:
    """Приложение не тянет тяжёлые парсеры: они живут в телах инструментов."""

    @pytest.mark.parametrize(
        "module", ["liteparse", "markdownify", "bs4", "lxml", "plotly"]
    )
    def test_app_does_not_import(self, module: str) -> None:
        code = (
            "import sys\n"
            "import boba.chainlit.infra.plugins\n"
            f"assert {module!r} not in sys.modules, 'the app pulls {module}'\n"
            "print('ok')\n"
        )
        subprocess.run(  # noqa: S603
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
