"""Тесты парсинга Confluence-export'а на реальном файле local/manual/950276.html."""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from boba.adapter.fs_workspace.shell import FsProjectWorkspaceShell
from boba.ext.confluence._parse import (
    anchor_for,
    collect_headings,
    load_soup,
    resolve_anchor,
    strip_confluence_macros,
)
from boba.ext.confluence.outline import (
    ConfluenceOutlineTool,
    ConfluenceOutlineToolConfig,
)
from boba.ext.confluence.section import (
    ConfluenceSectionTool,
    ConfluenceSectionToolConfig,
)
from boba.tools import ToolContext, ToolExecutionError
from boba.workspace import WorkspaceId

_FIXTURE_DIR = Path("/app/docker/compose/boba/local/manual")
_FIXTURE_FILE = "950276.html"


@pytest.fixture(scope="module")
def soup() -> BeautifulSoup:
    with (_FIXTURE_DIR / _FIXTURE_FILE).open("rb") as f:
        return BeautifulSoup(f.read(), "lxml")


@pytest.fixture
def ctx() -> ToolContext:
    ws = FsProjectWorkspaceShell(WorkspaceId.new(), _FIXTURE_DIR)
    return ToolContext(project_workspace=ws)


def test_load_soup_via_workspace(ctx: ToolContext) -> None:
    """load_soup читает файл через workspace и возвращает BeautifulSoup."""
    s = load_soup(ctx.project_workspace, _FIXTURE_FILE)
    assert s.find("h1") is not None


def test_collect_headings_all(soup: BeautifulSoup) -> None:
    """В фикстуре 36 заголовков, индексы 1-based, монотонно растут."""
    hs = collect_headings(soup)
    assert len(hs) == 36
    assert [h.index for h in hs] == list(range(1, 37))


def test_first_heading_no_anchor(soup: BeautifulSoup) -> None:
    """Первый h1 содержит только _GoBack-anchor — он должен пропускаться."""
    h = collect_headings(soup)[0]
    assert h.level == 1
    assert h.text == "Правила именования"
    assert h.anchor is None


def test_heading_text_strips_macros(soup: BeautifulSoup) -> None:
    """Текст заголовка не содержит мусора от ac:* макросов."""
    hs = collect_headings(soup)
    for h in hs:
        assert "scroll-bookmark" not in h.text
        assert "_GoBack" not in h.text


def test_heading_anchors(soup: BeautifulSoup) -> None:
    """Anchor извлекается из ac:structured-macro ac:name='anchor'."""
    hs = collect_headings(soup)
    by_text = {h.text: h for h in hs}
    assert by_text["Правила именования AD групп"].anchor == "scroll-bookmark-2"
    assert by_text["Группы доступа домены данных УпД"].anchor == "scroll-bookmark-5"


def test_max_depth_filters_levels(soup: BeautifulSoup) -> None:
    """max_depth=1 оставляет только h1; max_depth=2 — h1+h2."""
    h1_only = collect_headings(soup, max_depth=1)
    assert len(h1_only) == 4
    assert {h.level for h in h1_only} == {1}

    h12 = collect_headings(soup, max_depth=2)
    assert {h.level for h in h12} <= {1, 2}
    assert len(h12) > len(h1_only)


def test_anchor_for_fallback(soup: BeautifulSoup) -> None:
    """Без confluence-anchor anchor_for отдаёт idx:N."""
    hs = collect_headings(soup)
    assert anchor_for(hs[0]) == "idx:1"
    assert anchor_for(hs[1]) == "scroll-bookmark-2"


def test_resolve_anchor_variants(soup: BeautifulSoup) -> None:
    """resolve_anchor принимает scroll-bookmark-N / idx:N / с ведущим #."""
    hs = collect_headings(soup)
    target = next(h for h in hs if h.anchor == "scroll-bookmark-2")
    assert resolve_anchor(hs, "scroll-bookmark-2") is target
    assert resolve_anchor(hs, "#scroll-bookmark-2") is target
    assert resolve_anchor(hs, "idx:1") is hs[0]
    assert resolve_anchor(hs, "#idx:1") is hs[0]


def test_resolve_anchor_unknown(soup: BeautifulSoup) -> None:
    """Несуществующий или невалидный idx — None."""
    hs = collect_headings(soup)
    assert resolve_anchor(hs, "scroll-bookmark-9999") is None
    assert resolve_anchor(hs, "idx:abc") is None
    assert resolve_anchor(hs, "idx:9999") is None


def test_strip_confluence_macros_removes_ac_ri() -> None:
    """ac:* и ri:* теги удаляются полностью; обычная разметка остаётся."""
    src = (
        "<p>before"
        "<ac:structured-macro ac:name='anchor'>"
        "<ac:parameter>scroll-bookmark-1</ac:parameter>"
        "</ac:structured-macro>"
        "after</p>"
        "<p><ri:attachment ri:filename='x.png'/>tail</p>"
    )
    out = strip_confluence_macros(src)
    assert "ac:" not in out
    assert "ri:" not in out
    assert "before" in out
    assert "after" in out
    assert "tail" in out


def test_strip_confluence_macros_keeps_plain_html() -> None:
    """Без confluence-макросов выход эквивалентен входу по содержимому."""
    src = "<p>hi</p><ul><li>a</li><li>b</li></ul>"
    out = strip_confluence_macros(src)
    assert "<p>hi</p>" in out
    assert "<li>a</li>" in out
    assert "<li>b</li>" in out


def test_outline_tool_renders_full_doc(ctx: ToolContext) -> None:
    """confluence_outline возвращает все 36 заголовков с anchor'ами."""
    tool = ConfluenceOutlineTool(ConfluenceOutlineToolConfig(description=ConfluenceOutlineTool.DEFAULT_DESCRIPTION))
    args = tool.args_converter().convert(
        {"path": _FIXTURE_FILE, "limit": 200}
    )
    res = tool.execute(ctx, args)
    assert "Заголовков: 36" in res.content
    assert "scroll-bookmark-2" in res.content
    assert "#idx:1" in res.content
    assert "  1. h1 Правила именования" in res.content


def test_outline_tool_max_depth(ctx: ToolContext) -> None:
    """max_depth=1 в outline-tool ограничивает выдачу до h1."""
    tool = ConfluenceOutlineTool(ConfluenceOutlineToolConfig(description=ConfluenceOutlineTool.DEFAULT_DESCRIPTION))
    args = tool.args_converter().convert(
        {"path": _FIXTURE_FILE, "max_depth": 1, "limit": 200}
    )
    res = tool.execute(ctx, args)
    assert "Заголовков: 4" in res.content
    assert "h2 " not in res.content


def test_outline_tool_limit_truncates(ctx: ToolContext) -> None:
    """Если headings больше limit — добавляется суффикс truncated."""
    tool = ConfluenceOutlineTool(ConfluenceOutlineToolConfig(description=ConfluenceOutlineTool.DEFAULT_DESCRIPTION))
    args = tool.args_converter().convert(
        {"path": _FIXTURE_FILE, "limit": 5}
    )
    res = tool.execute(ctx, args)
    assert "truncated at limit=5" in res.content


def test_outline_tool_missing_file(ctx: ToolContext) -> None:
    """Отсутствующий файл — ToolExecutionError, не CRASH."""
    tool = ConfluenceOutlineTool(ConfluenceOutlineToolConfig(description=ConfluenceOutlineTool.DEFAULT_DESCRIPTION))
    args = tool.args_converter().convert(
        {"path": "no-such-file.html", "limit": 200}
    )
    with pytest.raises(ToolExecutionError):
        tool.execute(ctx, args)


def test_section_tool_returns_section(ctx: ToolContext) -> None:
    """Раздел по anchor возвращает HTML начиная с заголовка."""
    tool = ConfluenceSectionTool(ConfluenceSectionToolConfig(description=ConfluenceSectionTool.DEFAULT_DESCRIPTION))
    args = tool.args_converter().convert(
        {
            "path": _FIXTURE_FILE,
            "anchor": "scroll-bookmark-2",
            "include_subsections": True,
            "strip_macros": True,
            "max_chars": 8000,
        }
    )
    res = tool.execute(ctx, args)
    assert res.content.lstrip().startswith("<h1>")
    assert "Правила именования AD групп" in res.content
    # strip_macros=True → реальных ac:*/ri:* тегов в HTML не остаётся
    # (escaped &lt;ac:..&gt; внутри текста кодовых блоков — это контент, не тег).
    assert "<ac:" not in res.content
    assert "<ri:" not in res.content


def test_section_tool_include_subsections_stops_at_same_level(
    ctx: ToolContext,
) -> None:
    """include_subsections=True: для h1 stop — на следующем h1, h2-подразделы внутри."""
    tool = ConfluenceSectionTool(ConfluenceSectionToolConfig(description=ConfluenceSectionTool.DEFAULT_DESCRIPTION))
    args = tool.args_converter().convert(
        {
            "path": _FIXTURE_FILE,
            "anchor": "scroll-bookmark-2",
            "include_subsections": True,
            "strip_macros": False,
            "max_chars": 200000,
        }
    )
    res = tool.execute(ctx, args)
    # h2-подразделы scroll-bookmark-5/-6 относятся к этому h1 → внутри секции.
    assert "scroll-bookmark-5" in res.content
    assert "scroll-bookmark-6" in res.content
    # Следующий h1 (scroll-bookmark-3) не должен попасть в секцию.
    assert "scroll-bookmark-3" not in res.content


def test_section_tool_no_subsections_stops_at_first_heading(
    ctx: ToolContext,
) -> None:
    """include_subsections=False: stop на первом же следующем heading'е (любого уровня)."""
    tool = ConfluenceSectionTool(ConfluenceSectionToolConfig(description=ConfluenceSectionTool.DEFAULT_DESCRIPTION))
    base_args = {
        "path": _FIXTURE_FILE,
        "anchor": "scroll-bookmark-2",
        "include_subsections": False,
        "strip_macros": False,
        "max_chars": 200000,
    }
    no_subs = tool.execute(ctx, tool.args_converter().convert(base_args))
    with_subs = tool.execute(
        ctx,
        tool.args_converter().convert(
            {**base_args, "include_subsections": True}
        ),
    )
    # без subsections внутри только сам h1; в варианте с subsections — плюс h2.
    assert no_subs.content.count("<h2>") == 0
    assert with_subs.content.count("<h2>") >= 2
    assert len(no_subs.content) < len(with_subs.content)


def test_section_tool_idx_anchor_works(ctx: ToolContext) -> None:
    """idx:N тоже валидный anchor для secsion-tool."""
    tool = ConfluenceSectionTool(ConfluenceSectionToolConfig(description=ConfluenceSectionTool.DEFAULT_DESCRIPTION))
    args = tool.args_converter().convert(
        {
            "path": _FIXTURE_FILE,
            "anchor": "idx:1",
            "include_subsections": False,
            "strip_macros": True,
            "max_chars": 4000,
        }
    )
    res = tool.execute(ctx, args)
    assert "Правила именования" in res.content


def test_section_tool_unknown_anchor_raises(ctx: ToolContext) -> None:
    """Несуществующий anchor — ToolExecutionError."""
    tool = ConfluenceSectionTool(ConfluenceSectionToolConfig(description=ConfluenceSectionTool.DEFAULT_DESCRIPTION))
    args = tool.args_converter().convert(
        {
            "path": _FIXTURE_FILE,
            "anchor": "scroll-bookmark-does-not-exist",
            "include_subsections": True,
            "strip_macros": True,
            "max_chars": 8000,
        }
    )
    with pytest.raises(ToolExecutionError):
        tool.execute(ctx, args)


def test_section_tool_max_chars_truncates(ctx: ToolContext) -> None:
    """При превышении max_chars в конец добавляется суффикс truncated."""
    tool = ConfluenceSectionTool(ConfluenceSectionToolConfig(description=ConfluenceSectionTool.DEFAULT_DESCRIPTION))
    args = tool.args_converter().convert(
        {
            "path": _FIXTURE_FILE,
            "anchor": "scroll-bookmark-2",
            "include_subsections": True,
            "strip_macros": False,
            "max_chars": 200,
        }
    )
    res = tool.execute(ctx, args)
    assert "truncated at max_chars=200" in res.content


def test_tool_ids() -> None:
    """ToolId.name отражает имя инструмента."""
    assert ConfluenceOutlineTool(ConfluenceOutlineToolConfig(description=ConfluenceOutlineTool.DEFAULT_DESCRIPTION)).tool_id().name == "confluence_outline"
    assert ConfluenceSectionTool(ConfluenceSectionToolConfig(description=ConfluenceSectionTool.DEFAULT_DESCRIPTION)).tool_id().name == "confluence_section"
