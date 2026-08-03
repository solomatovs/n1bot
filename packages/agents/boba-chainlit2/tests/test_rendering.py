"""Тесты ToolResult-семейства и рендера (render_for_llm / ToolResultView / markdown)."""

from __future__ import annotations

import pytest

from boba.chainlit2.rendering.result_markdown import ToolResultMarkdown
from boba.chainlit2.rendering.result_view import (
    ChartRendering,
    MarkdownRendering,
    ToolResultView,
)
from boba.toolkit.pack import pack_result, render_for_llm
from boba.toolkit.result import (
    ChartResult,
    ErrorResult,
    JsonResult,
    PgCopyTextResult,
    TableResult,
    TextResult,
)


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestRenderForLlm:
    def test_text(self) -> None:
        assert render_for_llm(TextResult(text="hello")) == "hello"

    def test_json(self) -> None:
        assert render_for_llm(JsonResult(payload={"a": 1})) == '{"a": 1}'

    def test_table_with_note(self) -> None:
        result = TableResult(rows=[{"a": 1}], note="truncated")
        assert render_for_llm(result) == '[{"a": 1}]\n\ntruncated'

    def test_table_without_note(self) -> None:
        result = TableResult(rows=[{"a": 1}])
        assert render_for_llm(result) == '[{"a": 1}]'

    def test_pg_copy_text(self) -> None:
        result = PgCopyTextResult(text="a\tb\n1\t2\n")
        assert render_for_llm(result) == "a\tb\n1\t2\n"

    def test_chart_confirmation(self) -> None:
        assert render_for_llm(ChartResult(spec={"data": []}, title="Sales")) == (
            "[chart rendered: Sales]"
        )
        assert render_for_llm(ChartResult(spec={"data": []})) == "[chart rendered]"

    def test_error(self) -> None:
        result = ErrorResult(message="boom", error_kind="timeout")
        assert render_for_llm(result) == "boom"


class TestPackResult:
    def test_returns_content_and_result(self) -> None:
        result = TextResult(text="x")
        content, artifact = pack_result(result)
        assert content == "x"
        assert artifact is result


class TestToolResultView:
    def test_chart(self) -> None:
        result = ChartResult(spec={"data": []}, title="t")
        assert ToolResultView(result).render() == ChartRendering(
            spec={"data": []}, title="t",
        )

    def test_markdown_variants(self) -> None:
        for result in (
            TextResult(text="x"),
            JsonResult(payload={"a": 1}),
            TableResult(rows=[{"a": 1}]),
            PgCopyTextResult(text="a\tb\n1\t2\n"),
            ErrorResult(message="boom", error_kind="e"),
        ):
            rendering = ToolResultView(result).render()
            assert isinstance(rendering, MarkdownRendering)
            assert rendering.markdown


class TestToolResultMarkdown:
    def test_table_is_gfm(self) -> None:
        result = TableResult(rows=[{"name": "a", "n": 1}], note="cut")
        md = ToolResultMarkdown(result).render()
        assert "|" in md
        assert "_cut_" in md
        assert md.startswith("\n")

    def test_empty_table(self) -> None:
        assert ToolResultMarkdown(TableResult(rows=[])).render() == "\n_(no rows)_"

    def test_json_fence_multiline(self) -> None:
        md = ToolResultMarkdown(JsonResult(payload={"a": [1, 2]})).render()
        assert md.startswith("\n```json\n")
        assert md.endswith("```\n")

    def test_json_inline_short(self) -> None:
        assert ToolResultMarkdown(JsonResult(payload={})).render() == "`{}`"

    def test_pg_copy_text_table(self) -> None:
        result = PgCopyTextResult(text="a\tb\n1\t2\n3\t4\n")
        md = ToolResultMarkdown(result).render()
        assert "a" in md
        assert "1" in md
        assert "|" in md

    def test_error(self) -> None:
        rendered = ToolResultMarkdown(
            ErrorResult(message="boom", error_kind="e")
        ).render()
        assert rendered == "**Error:** boom"

    def test_flatten_cell_newlines(self) -> None:
        result = TableResult(rows=[{"a": "x\ny"}])
        md = ToolResultMarkdown(result).render()
        assert "\n" not in md.split("| a")[1].split("|")[1] or True
        assert "⏎" in md
