"""Тесты семейств вызова и результата: llm_view, блоки ответа для человека
по таблице 7.4 и рендер входа."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

import mcp_types as mt

from boba.toolkit.calls import FieldMarks, ToolCallBase, ToolCallModels
from boba.toolkit.dag import DagNode, NodeOutcomes
from boba.toolkit.result import (
    ErrorResult,
    MarkdownResult,
    ShellResult,
    SqlResult,
    SqlStatement,
    TableResult,
    ToolArtifact,
    ToolResultBase,
    VisualResult,
)
from boba.toolkit.wire import ResultWire


def shown(result: ToolResultBase) -> list[tuple[str, str, str]]:
    """Блоки ответа для человека: (вид, MIME и имя ресурса, текст)."""
    call = DagNode(key="call", tool="probe")
    wire = ResultWire().packed(NodeOutcomes().of(call, result, False))

    blocks: list[tuple[str, str, str]] = []
    for block in wire.content:
        annotations = block.annotations
        if annotations is None:
            annotations = mt.Annotations()

        if annotations.audience == ["assistant"]:
            continue

        if isinstance(block, mt.TextContent):
            blocks.append(("text", "", block.text))
            continue

        if isinstance(block, mt.EmbeddedResource):
            resource = block.resource
            if not isinstance(resource, mt.TextResourceContents):
                raise AssertionError(f"a resource of the family is text: {block}")

            name = unquote(PurePosixPath(urlsplit(str(resource.uri)).path).name)
            blocks.append(("resource", f"{resource.mime_type} {name}", resource.text))
            continue

        raise AssertionError(f"unexpected block: {block}")

    return blocks


def shell_result(**overrides: object) -> ShellResult:
    """Итог команды с чистым прогоном; тест меняет только то, что проверяет."""
    fields: dict[str, object] = {
        "exit_code": 0,
        "stdout": "total 0\n",
        "stdout_bytes": 9,
        "stdout_truncated": False,
        "stderr": "",
        "stderr_bytes": 0,
        "stderr_truncated": False,
        "duration_ms": 12,
        "timed_out": False,
    }
    fields.update(overrides)

    return ShellResult.model_validate(fields)


class TestRenderForLlm:
    def test_text(self) -> None:
        if MarkdownResult(text="hello").llm_view() != "hello":
            raise AssertionError('MarkdownResult(text="hello").llm_view() == "hello"')

    def test_table_with_note(self) -> None:
        result = TableResult(rows=[{"a": 1}], note="truncated")
        if result.llm_view() != '[{"a": 1}]\n\ntruncated':
            raise AssertionError("result.llm_view() == '[{\"a\": 1}]\\n\\ntruncated'")

    def test_table_without_note(self) -> None:
        result = TableResult(rows=[{"a": 1}])
        if result.llm_view() != '[{"a": 1}]':
            raise AssertionError("result.llm_view() == '[{\"a\": 1}]'")

    def test_chart_confirmation(self) -> None:
        titled = VisualResult.plotly({"data": []}, "Sales").llm_view()
        if titled != "[plotly rendered: Sales]":
            raise AssertionError('titled == "[plotly rendered: Sales]"')

        untitled = VisualResult.plotly({"data": []}, None).llm_view()
        if untitled != "[plotly rendered]":
            raise AssertionError('untitled == "[plotly rendered]"')

    def test_error(self) -> None:
        result = ErrorResult(message="boom", error_kind="timeout")
        if result.llm_view() != "boom":
            raise AssertionError('result.llm_view() == "boom"')


class TestPacked:
    def test_returns_content_and_result(self) -> None:
        result = MarkdownResult(text="x")
        content, artifact = result.packed()
        if content != "x":
            raise AssertionError('content == "x"')
        if artifact is not result:
            raise AssertionError("artifact is result")


class TestShownBlocks:
    def test_chart_is_a_caption_and_its_figure(self) -> None:
        result = VisualResult.plotly({"data": []}, "t")

        if shown(result) != [
            ("text", "", "_(plotly: t)_"),
            ("resource", "application/vnd.plotly.v1+json t", '{"data": []}'),
        ]:
            raise AssertionError(shown(result))

    def test_table_is_csv_with_the_note_below(self) -> None:
        result = TableResult(rows=[{"name": "a", "n": 1}], note="cut")

        if shown(result) != [
            ("resource", "text/csv csv", "name,n\na,1\n"),
            ("text", "", "_cut_"),
        ]:
            raise AssertionError(shown(result))

    def test_empty_table(self) -> None:
        if shown(TableResult(rows=[])) != [("resource", "text/csv csv", "")]:
            raise AssertionError(shown(TableResult(rows=[])))

    def test_error(self) -> None:
        rendered = shown(ErrorResult(message="boom", error_kind="e"))
        if rendered != [("text", "", "**boom**")]:
            raise AssertionError(f"rendered: {rendered!r}")

    def test_multiline_error_keeps_the_rest_in_a_block(self) -> None:
        message = "permission denied\nLINE 1: select 1\n       ^"
        rendered = shown(ErrorResult(message=message, error_kind="e"))
        expected = "**permission denied**\n\n```\nLINE 1: select 1\n       ^\n```"
        if rendered != [("text", "", expected)]:
            raise AssertionError(f"rendered: {rendered!r}")

    def test_flatten_cell_newlines(self) -> None:
        rendered = shown(TableResult(rows=[{"a": "x\ny"}]))
        if rendered != [("resource", "text/csv csv", "a\nx ⏎ y\n")]:
            raise AssertionError(f"rendered: {rendered!r}")


class TestShellResult:
    """Итог bash: команда скриптом, вывод под ней, служебное — в отчёт LLM."""

    def test_llm_report_keeps_both_streams(self) -> None:
        """LLM разбирает потоки сама: в отчёте они оба и код возврата."""
        result = shell_result(stdout="out\n", stderr="warn\n", stderr_bytes=5)

        report = json.loads(result.llm_view())

        if report["stdout"] != "out\n":
            raise AssertionError('report["stdout"] == "out\\n"')
        if report["stderr"] != "warn\n":
            raise AssertionError('report["stderr"] == "warn\\n"')
        if report["exit_code"] != 0:
            raise AssertionError('report["exit_code"] == 0')

    def test_output_prefers_stdout(self) -> None:
        result = shell_result(stdout="out\n", stderr="warn\n", stderr_bytes=5)

        if result.output != "out\n":
            raise AssertionError('result.output == "out\\n"')

    def test_output_falls_back_to_stderr(self) -> None:
        """Пустой stdout уступает место stderr: иначе ошибка команды не видна."""
        result = shell_result(
            stdout="   \n", stdout_bytes=4, stderr="boom\n", stderr_bytes=5
        )

        if result.output != "boom\n":
            raise AssertionError('result.output == "boom\\n"')

    def test_truncation_follows_the_shown_stream(self) -> None:
        result = shell_result(
            stdout="",
            stdout_bytes=0,
            stdout_truncated=False,
            stderr="boom\n",
            stderr_bytes=99,
            stderr_truncated=True,
        )

        if not result.truncated:
            raise AssertionError("result.truncated")

    def test_stdout_is_a_resource_with_the_exit_code_below(self) -> None:
        """Ресурс с именем потока, код возврата строкой под ним; команды нет."""
        rendered = shown(shell_result(stdout="total 0\n"))

        if rendered != [
            ("resource", "text/plain stdout", "total 0"),
            ("text", "", "_exit code: 0_"),
        ]:
            raise AssertionError(f"rendered: {rendered!r}")

    def test_markdown_of_a_killed_process(self) -> None:
        """Отрицательный код — процесс убит сигналом, а не «exit code: -9»."""
        rendered = shown(shell_result(exit_code=-9, stdout="partial\n"))

        if rendered[-1] != ("text", "", "_killed by signal 9_"):
            raise AssertionError(f"rendered: {rendered!r}")

    def test_stderr_is_shown_when_stdout_is_empty(self) -> None:
        result = shell_result(
            exit_code=2,
            stdout="",
            stdout_bytes=0,
            stderr="ls: no such file\n",
            stderr_bytes=17,
        )

        if shown(result) != [
            ("resource", "text/plain stderr", "ls: no such file"),
            ("text", "", "_exit code: 2_"),
        ]:
            raise AssertionError(f"rendered: {shown(result)!r}")

    def test_markdown_marks_silent_command(self) -> None:
        """Молчат оба потока: блока нет, код возврата остаётся на виду."""
        result = shell_result(stdout="", stdout_bytes=0, exit_code=7)

        if shown(result) != [
            ("text", "", "_(no output)_"),
            ("text", "", "_exit code: 7_"),
        ]:
            raise AssertionError(f"rendered: {shown(result)!r}")

    def test_markdown_collects_notes(self) -> None:
        result = shell_result(
            exit_code=124,
            stdout="head\n",
            stdout_truncated=True,
            timed_out=True,
        )

        md = shown(result)[-1][2]

        if "timed out" not in md:
            raise AssertionError('"timed out" in md')
        if "exit code: 124" not in md:
            raise AssertionError('"exit code: 124" in md')
        if "output truncated" not in md:
            raise AssertionError('"output truncated" in md')

    def test_output_with_backticks_travels_as_is(self) -> None:
        """Ограду ставит лента: ресурс несёт вывод без изменений."""
        rendered = shown(shell_result(stdout="```\nnested\n```\n"))

        if rendered[0] != ("resource", "text/plain stdout", "```\nnested\n```"):
            raise AssertionError(f"rendered: {rendered!r}")


class TestToolCallMarkdown:
    """Вход шага рисует модель вызова из объявленных у полей результатов."""

    @staticmethod
    def _call(name: str, args: dict[str, Any]) -> ToolCallBase:
        return ToolCallModels.call_of(name, args)

    @staticmethod
    def _declare_bash() -> None:
        """Импорт модуля bash объявляет его модель вызова."""
        from boba.tool.shell.tools import TOOLS

        if not TOOLS:
            raise AssertionError("shell module declared its tools")

    def test_unknown_tool_renders_json(self) -> None:
        markdown = self._call("no_such_tool", {"path": "/workspace/a.png"}).markdown()

        if not markdown.startswith("```json\n{"):
            raise AssertionError('markdown.startswith("```json\\n{")')

    def test_bash_renders_a_language_block(self) -> None:
        self._declare_bash()
        markdown = self._call("bash", {"command": "ls -la"}).markdown()

        if markdown != "```bash\nls -la\n```":
            raise AssertionError(
                f'markdown == "```bash\\nls -la\\n```", дано {markdown!r}'
            )

    def test_missing_argument_is_skipped(self) -> None:
        """Аргументы разошлись со схемой: показывается то, что пришло."""
        self._declare_bash()
        markdown = self._call("bash", {"path": "/workspace/a.png"}).markdown()

        if markdown != "":
            raise AssertionError(f"пустой вход, дано {markdown!r}")

    def test_sql_input_renders_as_a_sql_block(self) -> None:
        from boba.tool.pg.tools import TOOLS

        if not TOOLS:
            raise AssertionError("pg module declared its tools")
        markdown = self._call(
            "pg_query", {"connection": "dwh", "sql": "select 1\nfrom t"}
        ).markdown()

        if "```sql\nselect 1\nfrom t\n```" not in markdown:
            raise AssertionError('"```sql\\nselect 1\\nfrom t\\n```" in markdown')
        if "**connection:** `dwh`" not in markdown:
            raise AssertionError('"**connection:** `dwh`" in markdown')

    def test_mermaid_spec_renders_as_a_mermaid_block(self) -> None:
        from boba.tool.canvas.tools import TOOLS

        if not TOOLS:
            raise AssertionError("canvas module declared its tools")
        markdown = self._call(
            "diagram_save", {"name": "a.mmd", "spec": "flowchart LR\n    A --> B"}
        ).markdown()

        if "```mermaid\nflowchart LR\n    A --> B\n```" not in markdown:
            raise AssertionError("спека рисуется mermaid-блоком")
        if "**name:** `a.mmd`" not in markdown:
            raise AssertionError('"**name:** `a.mmd`" in markdown')


class TestDeclaredDisplays:
    """Объявления показа живут у полей модулей тулов и видны через модель."""

    def test_module_declarations_register_through_the_facade(self) -> None:
        from boba.tool.ch.tools import TOOLS as CH_TOOLS
        from boba.tool.chart.tools import TOOLS as CHART_TOOLS
        from boba.tool.pg.tools import TOOLS as PG_TOOLS

        if not (PG_TOOLS and CH_TOOLS and CHART_TOOLS):
            raise AssertionError("модули отдали свои TOOLS")

        expected = {
            ("pg_query", "sql"): "sql",
            ("pg_stream_out", "sql"): "sql",
            ("ch_query", "sql"): "sql",
            ("visualize", "spec"): "json",
        }
        for (name, arg), language in expected.items():
            call = ToolCallModels.call_of(name, {arg: "x"})
            display = FieldMarks.display(type(call).model_fields[arg])
            if not isinstance(display, MarkdownResult):
                raise AssertionError(f"{name}.{arg}: display is MarkdownResult")
            if display.language != language:
                raise AssertionError(f"{name}.{arg}: language == {language!r}")


class TestTextResultLanguage:
    """Текст с языком уходит ресурсом своего MIME, как и объявленный показ
    аргумента; без языка — текстом."""

    def test_plain_text_stays_markdown(self) -> None:
        if shown(MarkdownResult(text="**bold**")) != [("text", "", "**bold**")]:
            raise AssertionError(shown(MarkdownResult(text="**bold**")))

    def test_language_is_the_mime_and_the_name_of_the_resource(self) -> None:
        result = MarkdownResult(text="one,two\n1,два\n", language="csv")

        if shown(result) != [("resource", "text/csv csv", "one,two\n1,два")]:
            raise AssertionError(shown(result))

    def test_llm_gets_the_text_without_the_fence(self) -> None:
        """Блок — дело показа: LLM получает дамп как есть."""
        result = MarkdownResult(text="one,two\n1,два\n", language="csv")

        if result.llm_view() != "one,two\n1,два\n":
            raise AssertionError("result.llm_view() == текст дампа")


class TestTextResultNote:
    """Подпись источника под текстом: окно строк страницы, сводка грепа."""

    def test_note_goes_under_the_resource(self) -> None:
        result = MarkdownResult(
            text="<p>hi</p>", language="html", note="url=x; lines 1-1"
        )

        if shown(result) != [
            ("resource", "text/plain html", "<p>hi</p>"),
            ("text", "", "_url=x; lines 1-1_"),
        ]:
            raise AssertionError(f"подпись под блоком, получено {shown(result)!r}")

    def test_empty_text_leaves_only_the_note(self) -> None:
        """Греп без совпадений: пустого ресурса нет."""
        result = MarkdownResult(
            text="", language="text", note="url=x: no matches found"
        )

        if shown(result) != [("text", "", "_url=x: no matches found_")]:
            raise AssertionError(f"одна подпись, получено {shown(result)!r}")

    def test_llm_gets_the_note_after_the_text(self) -> None:
        result = MarkdownResult(text="page", note="url=x; lines 1-1 of 9")

        if result.llm_view() != "page\n\nurl=x; lines 1-1 of 9":
            raise AssertionError("подпись уходит в LLM отдельным абзацем")

    def test_llm_gets_only_the_note_when_text_is_empty(self) -> None:
        result = MarkdownResult(text="", note="url=x: no matches found")

        if result.llm_view() != "url=x: no matches found":
            raise AssertionError("пустой текст не даёт пустых абзацев")


class TestSqlResult:
    """Итог запроса: команды рисуются своими блоками под подписью статуса."""

    @staticmethod
    def _both() -> SqlResult:
        return SqlResult(
            engine="postgres",
            statements=(
                SqlStatement(rows=[{"blobs": 3}], status="SELECT 1"),
                SqlStatement(affected_rows=5, status="DELETE 5"),
            ),
        )

    def test_llm_gets_every_statement_captioned(self) -> None:
        report = self._both().llm_view()

        if not report.startswith("SELECT 1\n"):
            raise AssertionError('report.startswith("SELECT 1\\n")')
        if '[{"blobs": 3}]' not in report:
            raise AssertionError("выдача первой команды в отчёте")
        if not report.endswith("\n\nDELETE 5"):
            raise AssertionError(f"status statement last, captioned once: {report!r}")
        if report.count("DELETE 5") != 1:
            raise AssertionError(f"caption repeated: {report!r}")

    def test_blocks_keep_each_kind_of_statement(self) -> None:
        if shown(self._both()) != [
            ("text", "", "_SELECT 1_"),
            ("resource", "text/csv csv", "blobs\n3\n"),
            ("text", "", "_DELETE 5_"),
        ]:
            raise AssertionError(shown(self._both()))

    def test_result_survives_the_artifact_round_trip(self) -> None:
        """Итог персистится в checkpointer: команды оживают."""
        revived = ToolArtifact.revive(self._both().model_dump(mode="json"))

        if not isinstance(revived, SqlResult):
            raise AssertionError("isinstance(revived, SqlResult)")
        if len(revived.statements) != 2:
            raise AssertionError("len(revived.statements) == 2")
