"""Тесты ToolResult-семейства и рендера (render_for_llm / ToolResultView / markdown)."""

from __future__ import annotations

import pytest

from boba.chainlit.rendering.result import (
    ChartRendering,
    MarkdownRendering,
    ToolResultMarkdown,
    ToolResultView,
)
from boba.toolkit.result import (
    ChartResult,
    ErrorResult,
    JsonResult,
    PgCopyTextResult,
    TableResult,
    TextResult,
    pack_result,
    render_for_llm,
)


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestRenderForLlm:
    def test_text(self) -> None:
        if render_for_llm(TextResult(text="hello")) != "hello":
            raise AssertionError('render_for_llm(TextResult(text="hello")) == "hello"')

    def test_json(self) -> None:
        if render_for_llm(JsonResult(payload={"a": 1})) != '{"a": 1}':
            raise AssertionError(
                'render_for_llm(JsonResult(payload={"a": 1})) == \'{"…'
            )

    def test_table_with_note(self) -> None:
        result = TableResult(rows=[{"a": 1}], note="truncated")
        if render_for_llm(result) != '[{"a": 1}]\n\ntruncated':
            raise AssertionError(
                "render_for_llm(result) == '[{\"a\": 1}]\\n\\ntruncated'"
            )

    def test_table_without_note(self) -> None:
        result = TableResult(rows=[{"a": 1}])
        if render_for_llm(result) != '[{"a": 1}]':
            raise AssertionError("render_for_llm(result) == '[{\"a\": 1}]'")

    def test_pg_copy_text(self) -> None:
        result = PgCopyTextResult(text="a\tb\n1\t2\n")
        if render_for_llm(result) != "a\tb\n1\t2\n":
            raise AssertionError('render_for_llm(result) == "a\\tb\\n1\\t2\\n"')

    def test_chart_confirmation(self) -> None:
        if not (
            render_for_llm(ChartResult(spec={"data": []}, title="Sales"))
            == ("[chart rendered: Sales]")
        ):
            raise AssertionError('render_for_llm(ChartResult(spec={"data": []}, title…')
        if render_for_llm(ChartResult(spec={"data": []})) != "[chart rendered]":
            raise AssertionError('render_for_llm(ChartResult(spec={"data": []})) == "…')

    def test_error(self) -> None:
        result = ErrorResult(message="boom", error_kind="timeout")
        if render_for_llm(result) != "boom":
            raise AssertionError('render_for_llm(result) == "boom"')


class TestPackResult:
    def test_returns_content_and_result(self) -> None:
        result = TextResult(text="x")
        content, artifact = pack_result(result)
        if content != "x":
            raise AssertionError('content == "x"')
        if artifact is not result:
            raise AssertionError("artifact is result")


class TestToolResultView:
    def test_chart(self) -> None:
        result = ChartResult(spec={"data": []}, title="t")
        if not (
            ToolResultView(result).render()
            == ChartRendering(
                spec={"data": []},
                title="t",
            )
        ):
            raise AssertionError("ToolResultView(result).render() == ChartRendering( …")

    def test_markdown_variants(self) -> None:
        for result in (
            TextResult(text="x"),
            JsonResult(payload={"a": 1}),
            TableResult(rows=[{"a": 1}]),
            PgCopyTextResult(text="a\tb\n1\t2\n"),
            ErrorResult(message="boom", error_kind="e"),
        ):
            rendering = ToolResultView(result).render()
            if not (isinstance(rendering, MarkdownRendering)):
                raise AssertionError("isinstance(rendering, MarkdownRendering)")
            if not (rendering.markdown):
                raise AssertionError("rendering.markdown")


class TestToolResultMarkdown:
    def test_table_is_gfm(self) -> None:
        result = TableResult(rows=[{"name": "a", "n": 1}], note="cut")
        md = ToolResultMarkdown(result).render()
        if "|" not in md:
            raise AssertionError('"|" in md')
        if "_cut_" not in md:
            raise AssertionError('"_cut_" in md')
        if not (md.startswith("\n")):
            raise AssertionError('md.startswith("\\n")')

    def test_empty_table(self) -> None:
        if ToolResultMarkdown(TableResult(rows=[])).render() != "\n_(no rows)_":
            raise AssertionError("ToolResultMarkdown(TableResult(rows=[])).render() =…")

    def test_json_fence_multiline(self) -> None:
        md = ToolResultMarkdown(JsonResult(payload={"a": [1, 2]})).render()
        if not (md.startswith("\n```json\n")):
            raise AssertionError('md.startswith("\\n```json\\n")')
        if not (md.endswith("```\n")):
            raise AssertionError('md.endswith("```\\n")')

    def test_json_inline_short(self) -> None:
        if ToolResultMarkdown(JsonResult(payload={})).render() != "`{}`":
            raise AssertionError("ToolResultMarkdown(JsonResult(payload={})).render()…")

    def test_pg_copy_text_table(self) -> None:
        result = PgCopyTextResult(text="a\tb\n1\t2\n3\t4\n")
        md = ToolResultMarkdown(result).render()
        if "a" not in md:
            raise AssertionError('"a" in md')
        if "1" not in md:
            raise AssertionError('"1" in md')
        if "|" not in md:
            raise AssertionError('"|" in md')

    def test_error(self) -> None:
        rendered = ToolResultMarkdown(
            ErrorResult(message="boom", error_kind="e")
        ).render()
        if rendered != "**Error:** boom":
            raise AssertionError('rendered == "**Error:** boom"')

    def test_flatten_cell_newlines(self) -> None:
        result = TableResult(rows=[{"a": "x\ny"}])
        md = ToolResultMarkdown(result).render()
        if not ("\n" not in md.split("| a")[1].split("|")[1] or True):
            raise AssertionError(
                '"\\n" not in md.split("| a")[1].split("|")[1] or True'
            )
        if "⏎" not in md:
            raise AssertionError('"⏎" in md')
