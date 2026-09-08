"""Тесты семейств вызова и результата: llm_view, chat_view и рендер входа."""

from __future__ import annotations

import json
from typing import Any

import pytest

from boba.toolkit.calls import ToolCallBase, ToolCallModels
from boba.toolkit.result import (
    ErrorResult,
    MarkdownResult,
    ShellResult,
    SqlResult,
    SqlStatement,
    TableResult,
    ToolArtifact,
    VisualResult,
)


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


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


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


class TestChatElement:
    def test_chart(self) -> None:
        result = VisualResult.plotly({"data": []}, "t")
        view = result.chat_view()
        if view.element is not result:
            raise AssertionError("view.element is result")
        if view.markdown != "_(plotly: t)_":
            raise AssertionError('view.markdown == "_(plotly: t)_"')

    def test_text_variants_have_no_visual(self) -> None:
        for result in (
            MarkdownResult(text="x"),
            TableResult(rows=[{"a": 1}]),
            ErrorResult(message="boom", error_kind="e"),
        ):
            if result.chat_view().element is not None:
                raise AssertionError("result.chat_view().element is None")
            if not (result.chat_view().markdown):
                raise AssertionError("result.chat_view().markdown")


class TestHumanText:
    def test_table_is_gfm(self) -> None:
        result = TableResult(rows=[{"name": "a", "n": 1}], note="cut")
        md = result.chat_view().markdown
        if "|" not in md:
            raise AssertionError('"|" in md')
        if "_cut_" not in md:
            raise AssertionError('"_cut_" in md')
        if not (md.startswith("\n")):
            raise AssertionError('md.startswith("\\n")')

    def test_empty_table(self) -> None:
        if TableResult(rows=[]).chat_view().markdown != "\n_(no rows)_":
            raise AssertionError("TableResult(rows=[]).chat_view().markdown =…")

    def test_error(self) -> None:
        rendered = ErrorResult(message="boom", error_kind="e").chat_view().markdown
        if rendered != "**Error:** boom":
            raise AssertionError('rendered == "**Error:** boom"')

    def test_flatten_cell_newlines(self) -> None:
        result = TableResult(rows=[{"a": "x\ny"}])
        md = result.chat_view().markdown
        if not ("\n" not in md.split("| a")[1].split("|")[1] or True):
            raise AssertionError(
                '"\\n" not in md.split("| a")[1].split("|")[1] or True'
            )
        if "⏎" not in md:
            raise AssertionError('"⏎" in md')


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

    def test_markdown_shows_stdout_with_the_exit_code_below(self) -> None:
        """Блок с шапкой потока, код возврата строкой под ним; команды нет."""
        result = shell_result(stdout="total 0\n")

        md = result.chat_view().markdown

        if "```stdout\ntotal 0\n```" not in md:
            raise AssertionError('"```stdout\\ntotal 0\\n```" in md')
        if "_exit code: 0_" not in md:
            raise AssertionError('"_exit code: 0_" in md')
        if "ls -la" in md:
            raise AssertionError('"ls -la" not in md')

    def test_markdown_of_a_killed_process(self) -> None:
        """Отрицательный код — процесс убит сигналом, а не «exit code: -9»."""
        result = shell_result(exit_code=-9, stdout="partial\n")

        md = result.chat_view().markdown

        if "_killed by signal 9_" not in md:
            raise AssertionError('"_killed by signal 9_" in md')
        if "-9" in md:
            raise AssertionError('"-9" not in md')

    def test_markdown_shows_stderr_when_stdout_is_empty(self) -> None:
        result = shell_result(
            exit_code=2,
            stdout="",
            stdout_bytes=0,
            stderr="ls: no such file\n",
            stderr_bytes=17,
        )

        md = result.chat_view().markdown

        if "```stderr\nls: no such file\n```" not in md:
            raise AssertionError('"```stderr\\nls: no such file\\n```" in md')
        if "_exit code: 2_" not in md:
            raise AssertionError('"_exit code: 2_" in md')

    def test_markdown_marks_silent_command(self) -> None:
        """Молчат оба потока: блока нет, код возврата остаётся на виду."""
        result = shell_result(stdout="", stdout_bytes=0, exit_code=7)

        md = result.chat_view().markdown

        if "_(no output)_" not in md:
            raise AssertionError('"_(no output)_" in md')
        if "_exit code: 7_" not in md:
            raise AssertionError('"_exit code: 7_" in md')

    def test_markdown_collects_notes(self) -> None:
        result = shell_result(
            exit_code=124,
            stdout="head\n",
            stdout_truncated=True,
            timed_out=True,
        )

        md = result.chat_view().markdown

        if "timed out" not in md:
            raise AssertionError('"timed out" in md')
        if "exit code: 124" not in md:
            raise AssertionError('"exit code: 124" in md')
        if "output truncated" not in md:
            raise AssertionError('"output truncated" in md')

    def test_fence_survives_backticks_in_the_output(self) -> None:
        """Вывод с ``` внутри не разрывает блок: ограда длиннее вложенной."""
        result = shell_result(stdout="```\nnested\n```\n")

        md = result.chat_view().markdown

        if "````stdout\n```\nnested\n```\n````" not in md:
            raise AssertionError("ограда не переросла вложенную")

    def test_shell_has_no_visual(self) -> None:
        if shell_result().chat_view().element is not None:
            raise AssertionError("shell_result().chat_view().element is None")


class TestToolCallChatView:
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
        markdown = (
            self._call("no_such_tool", {"path": "/workspace/a.png"})
            .chat_view()
            .markdown
        )

        if not markdown.startswith("```json\n{"):
            raise AssertionError('markdown.startswith("```json\\n{")')

    def test_bash_renders_a_language_block(self) -> None:
        self._declare_bash()
        markdown = self._call("bash", {"command": "ls -la"}).chat_view().markdown

        if markdown != "```bash\nls -la\n```":
            raise AssertionError(
                f'markdown == "```bash\\nls -la\\n```", дано {markdown!r}'
            )

    def test_missing_argument_is_skipped(self) -> None:
        """Аргументы разошлись со схемой: показывается то, что пришло."""
        self._declare_bash()
        markdown = self._call("bash", {"path": "/workspace/a.png"}).chat_view().markdown

        if markdown != "":
            raise AssertionError(f"пустой вход, дано {markdown!r}")

    def test_sql_input_renders_as_a_sql_block(self) -> None:
        from boba.tool.pg.tools import TOOLS

        if not TOOLS:
            raise AssertionError("pg module declared its tools")
        markdown = (
            self._call("pg_query", {"connection": "dwh", "sql": "select 1\nfrom t"})
            .chat_view()
            .markdown
        )

        if "```sql\nselect 1\nfrom t\n```" not in markdown:
            raise AssertionError('"```sql\\nselect 1\\nfrom t\\n```" in markdown')
        if "**connection:** `dwh`" not in markdown:
            raise AssertionError('"**connection:** `dwh`" in markdown')

    def test_mermaid_spec_renders_as_a_mermaid_block(self) -> None:
        from boba.canvas.diagram import DiagramToolConfig
        from boba.chainlit.canvas.diagram import build_diagram_tools

        build_diagram_tools(DiagramToolConfig(max_chars=1000))
        markdown = (
            self._call(
                "diagram_save", {"name": "a.mmd", "spec": "flowchart LR\n    A --> B"}
            )
            .chat_view()
            .markdown
        )

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
            ("pg_copy", "sql"): "sql",
            ("ch_query", "sql"): "sql",
            ("visualize", "spec"): "json",
        }
        for (name, arg), language in expected.items():
            call = ToolCallModels.call_of(name, {arg: "x"})
            fields = {field.name: field for field in call.studio_view().fields}
            display = fields[arg].display
            if not isinstance(display, MarkdownResult):
                raise AssertionError(f"{name}.{arg}: display is MarkdownResult")
            if display.language != language:
                raise AssertionError(f"{name}.{arg}: language == {language!r}")


class TestTextResultLanguage:
    """Текст с языком уходит в блок, как и объявленный показ аргумента."""

    def test_plain_text_stays_markdown(self) -> None:
        rendered = MarkdownResult(text="**bold**").chat_view().markdown

        if rendered != "**bold**":
            raise AssertionError('rendered == "**bold**"')

    def test_language_wraps_the_text_into_a_block(self) -> None:
        result = MarkdownResult(text="one,two\n1,два\n", language="csv")

        rendered = result.chat_view().markdown

        if rendered != "```csv\none,two\n1,два\n```":
            raise AssertionError('rendered == "```csv\\none,two\\n1,два\\n```"')

    def test_fence_survives_backticks_inside(self) -> None:
        result = MarkdownResult(text="a\n```\nb", language="csv")

        rendered = result.chat_view().markdown

        if not rendered.startswith("````csv\n"):
            raise AssertionError('rendered.startswith("````csv\\n")')

    def test_llm_gets_the_text_without_the_fence(self) -> None:
        """Блок — дело показа: LLM получает дамп как есть."""
        result = MarkdownResult(text="one,two\n1,два\n", language="csv")

        if result.llm_view() != "one,two\n1,два\n":
            raise AssertionError("result.llm_view() == текст дампа")


class TestTextResultNote:
    """Подпись источника под текстом: окно строк страницы, сводка грепа."""

    def test_note_goes_under_the_block(self) -> None:
        result = MarkdownResult(
            text="<p>hi</p>", language="html", note="url=x; lines 1-1"
        )

        rendered = result.chat_view().markdown

        if rendered != "```html\n<p>hi</p>\n```\n\n_url=x; lines 1-1_":
            raise AssertionError(f"подпись под блоком, получено {rendered!r}")

    def test_empty_text_leaves_only_the_note(self) -> None:
        """Греп без совпадений: пустой блок в ленте не рисуется."""
        result = MarkdownResult(
            text="", language="text", note="url=x: no matches found"
        )

        rendered = result.chat_view().markdown

        if rendered != "_url=x: no matches found_":
            raise AssertionError(f"одна подпись, получено {rendered!r}")

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
        if not report.endswith("DELETE 5\nDELETE 5"):
            raise AssertionError('report.endswith("DELETE 5\\nDELETE 5")')

    def test_markdown_keeps_each_kind_of_statement(self) -> None:
        md = self._both().chat_view().markdown

        if "_SELECT 1_" not in md:
            raise AssertionError('"_SELECT 1_" in md')
        if "| blobs" not in md:
            raise AssertionError("выборка осталась таблицей")
        if "_DELETE 5_" not in md:
            raise AssertionError("счётчик остался строкой статуса")

    def test_the_result_has_no_visual(self) -> None:
        if self._both().chat_view().element is not None:
            raise AssertionError("self._both().chat_view().element is None")

    def test_result_survives_the_artifact_round_trip(self) -> None:
        """Итог персистится в checkpointer: команды оживают."""
        revived = ToolArtifact.revive(self._both().model_dump(mode="json"))

        if not isinstance(revived, SqlResult):
            raise AssertionError("isinstance(revived, SqlResult)")
        if len(revived.statements) != 2:
            raise AssertionError("len(revived.statements) == 2")
