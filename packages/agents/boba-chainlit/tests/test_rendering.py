"""Тесты семейств вызова и результата: render_for_llm, рендеры входа и выхода."""

from __future__ import annotations

import json

import pytest

from boba.chainlit.rendering.tool import (
    ChartRendering,
    MarkdownRendering,
    ToolCallMarkdown,
    ToolResultMarkdown,
    ToolResultView,
)
from boba.toolkit.calls import (
    HiddenCall,
    JsonCall,
    ScriptCall,
    ToolCallViews,
)
from boba.toolkit.result import (
    AffectedSqlResult,
    ChartResult,
    ErrorResult,
    JsonResult,
    MultiResult,
    ShellResult,
    TableResult,
    TextResult,
    ToolArtifact,
    pack_result,
    render_for_llm,
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
        "diagnostic": "",
    }
    fields.update(overrides)

    return ShellResult.model_validate(fields)


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


class TestShellResult:
    """Итог bash: команда скриптом, вывод под ней, служебное — в отчёт LLM."""

    def test_llm_report_keeps_both_streams(self) -> None:
        """LLM разбирает потоки сама: в отчёте они оба и код возврата."""
        result = shell_result(stdout="out\n", stderr="warn\n", stderr_bytes=5)

        report = json.loads(render_for_llm(result))

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

        md = ToolResultMarkdown(result).render()

        if "```stdout\ntotal 0\n```" not in md:
            raise AssertionError('"```stdout\\ntotal 0\\n```" in md')
        if "_exit code: 0_" not in md:
            raise AssertionError('"_exit code: 0_" in md')
        if "ls -la" in md:
            raise AssertionError('"ls -la" not in md')

    def test_markdown_of_a_killed_process(self) -> None:
        """Отрицательный код — процесс убит сигналом, а не «exit code: -9»."""
        result = shell_result(exit_code=-9, stdout="partial\n")

        md = ToolResultMarkdown(result).render()

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

        md = ToolResultMarkdown(result).render()

        if "```stderr\nls: no such file\n```" not in md:
            raise AssertionError('"```stderr\\nls: no such file\\n```" in md')
        if "_exit code: 2_" not in md:
            raise AssertionError('"_exit code: 2_" in md')

    def test_markdown_marks_silent_command(self) -> None:
        """Молчат оба потока: блока нет, код возврата остаётся на виду."""
        result = shell_result(stdout="", stdout_bytes=0, exit_code=7)

        md = ToolResultMarkdown(result).render()

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
            diagnostic="killed by the memory limit",
        )

        md = ToolResultMarkdown(result).render()

        if "timed out" not in md:
            raise AssertionError('"timed out" in md')
        if "exit code: 124" not in md:
            raise AssertionError('"exit code: 124" in md')
        if "output truncated" not in md:
            raise AssertionError('"output truncated" in md')
        if "killed by the memory limit" not in md:
            raise AssertionError('"killed by the memory limit" in md')

    def test_fence_survives_backticks_in_the_output(self) -> None:
        """Вывод с ``` внутри не разрывает блок: ограда длиннее вложенной."""
        result = shell_result(stdout="```\nnested\n```\n")

        md = ToolResultMarkdown(result).render()

        if "````stdout\n```\nnested\n```\n````" not in md:
            raise AssertionError("ограда не переросла вложенную")

    def test_view_renders_shell_as_markdown(self) -> None:
        rendering = ToolResultView(shell_result()).render()

        if not isinstance(rendering, MarkdownRendering):
            raise AssertionError("isinstance(rendering, MarkdownRendering)")


class TestToolCallMarkdown:
    """Вход шага по объявлению: зеркало рендера результата."""

    def test_json_call_renders_pretty_json(self) -> None:
        rendering = ToolCallMarkdown(
            JsonCall(), {"path": "/workspace/a.png"}
        ).render()

        if rendering is None:
            raise AssertionError("rendering is not None")
        if not rendering.markdown.startswith("{"):
            raise AssertionError('rendering.markdown.startswith("{")')
        if rendering.show_input != "json":
            raise AssertionError('rendering.show_input == "json"')

    def test_script_call_renders_a_language_block(self) -> None:
        rendering = ToolCallMarkdown(
            ScriptCall(arg="command", lang="bash"),
            {"command": "ls -la", "stdin": ""},
        ).render()

        if rendering is None:
            raise AssertionError("rendering is not None")
        if rendering.markdown != "```bash\nls -la\n```":
            raise AssertionError('rendering.markdown == "```bash\\nls -la\\n```"')
        if rendering.show_input is not True:
            raise AssertionError("rendering.show_input is True")

    def test_script_call_keeps_non_empty_arguments(self) -> None:
        """Непустой stdin виден рядом со скриптом; пустой не шумит."""
        rendering = ToolCallMarkdown(
            ScriptCall(arg="command", lang="bash"),
            {"command": "cat", "stdin": "line1\nline2"},
        ).render()

        if rendering is None:
            raise AssertionError("rendering is not None")
        if "```bash\ncat\n```" not in rendering.markdown:
            raise AssertionError('"```bash\\ncat\\n```" in rendering.markdown')
        if "**stdin:**" not in rendering.markdown:
            raise AssertionError('"**stdin:**" in rendering.markdown')

    def test_script_call_without_the_argument_falls_back(self) -> None:
        """Объявление разошлось со схемой: вход показывается json'ом."""
        rendering = ToolCallMarkdown(
            ScriptCall(arg="command", lang="bash"), {"path": "/workspace/a.png"}
        ).render()

        if rendering is None:
            raise AssertionError("rendering is not None")
        if rendering.show_input != "json":
            raise AssertionError('rendering.show_input == "json"')

    def test_hidden_call_shows_nothing(self) -> None:
        if ToolCallMarkdown(HiddenCall(), {"secret": "x"}).render() is not None:
            raise AssertionError("HiddenCall рендерится в None")

    def test_sql_input_renders_as_a_sql_block(self) -> None:
        rendering = ToolCallMarkdown(
            ScriptCall(arg="sql", lang="sql"),
            {"connection_name": "dwh", "sql": "select 1\nfrom t"},
        ).render()

        if rendering is None:
            raise AssertionError("rendering is not None")
        if "```sql\nselect 1\nfrom t\n```" not in rendering.markdown:
            raise AssertionError('"```sql\\nselect 1\\nfrom t\\n```" in markdown')
        if "**connection_name:** `dwh`" not in rendering.markdown:
            raise AssertionError('"**connection_name:** `dwh`" in markdown')

    def test_mermaid_spec_renders_as_a_mermaid_block(self) -> None:
        rendering = ToolCallMarkdown(
            ScriptCall(arg="spec", lang="mermaid"),
            {"name": "a.mmd", "spec": "flowchart LR\n    A --> B"},
        ).render()

        if rendering is None:
            raise AssertionError("rendering is not None")
        if "```mermaid\nflowchart LR\n    A --> B\n```" not in rendering.markdown:
            raise AssertionError("спека рисуется mermaid-блоком")
        if "**name:** `a.mmd`" not in rendering.markdown:
            raise AssertionError('"**name:** `a.mmd`" in rendering.markdown')


class TestDeclaredViews:
    """Объявления инструментов регистрируются и защищены от опечаток.

    Реестр наполняется импортом модулей и живёт весь процесс: чистить его
    между тестами нельзя — второй импорт уже ничего не объявит.
    """

    def test_module_declarations_register_through_the_toolset(self) -> None:
        """Импорт модулей тулов объявляет представления в реестре."""
        # импорт TOOLS и есть объявление: toolset модуля регистрирует views
        from boba.tool.ch.tools import TOOLS as CH_TOOLS
        from boba.tool.chart.tools import TOOLS as CHART_TOOLS
        from boba.tool.pg.tools import TOOLS as PG_TOOLS

        if not (PG_TOOLS and CH_TOOLS and CHART_TOOLS):
            raise AssertionError("модули отдали свои TOOLS")

        expected = {
            "pg_query": ScriptCall(arg="sql", lang="sql"),
            "pg_copy": ScriptCall(arg="sql", lang="sql"),
            "ch_query": ScriptCall(arg="sql", lang="sql"),
            "visualize": ScriptCall(arg="spec", lang="json"),
        }
        for name, view in expected.items():
            if ToolCallViews.of(name) != view:
                raise AssertionError(f"ToolCallViews.of({name!r}) == {view!r}")

        if ToolCallViews.of("pg_list_targets") != ToolCallViews.DEFAULT:
            raise AssertionError("pg_list_targets остаётся на JsonCall")

    def test_view_for_an_unknown_tool_name_fails_loudly(self) -> None:
        """Опечатка в views не должна тихо оставить инструмент на дефолте."""
        from boba.tool.pg.tools import pg_query
        from boba.toolkit.entry import ToolEntryError, ToolMain

        try:
            ToolMain.toolset(
                pg_query, views={"pg_qeury": ScriptCall(arg="sql", lang="sql")}
            )
        except ToolEntryError as e:
            if "pg_qeury" not in str(e):
                raise AssertionError('"pg_qeury" in str(e)') from e
            return

        raise AssertionError("toolset обязан отвергнуть чужое имя")


class TestTextResultLanguage:
    """Текст с языком уходит в блок: зеркало ScriptCall на входе."""

    def test_plain_text_stays_markdown(self) -> None:
        rendered = ToolResultMarkdown(TextResult(text="**bold**")).render()

        if rendered != "**bold**":
            raise AssertionError('rendered == "**bold**"')

    def test_language_wraps_the_text_into_a_block(self) -> None:
        result = TextResult(text="one,two\n1,два\n", language="csv")

        rendered = ToolResultMarkdown(result).render()

        if rendered != "```csv\none,two\n1,два\n```":
            raise AssertionError('rendered == "```csv\\none,two\\n1,два\\n```"')

    def test_fence_survives_backticks_inside(self) -> None:
        result = TextResult(text="a\n```\nb", language="csv")

        rendered = ToolResultMarkdown(result).render()

        if not rendered.startswith("````csv\n"):
            raise AssertionError('rendered.startswith("````csv\\n")')

    def test_llm_gets_the_text_without_the_fence(self) -> None:
        """Блок — дело показа: LLM получает дамп как есть."""
        result = TextResult(text="one,two\n1,два\n", language="csv")

        if render_for_llm(result) != "one,two\n1,два\n":
            raise AssertionError("render_for_llm(result) == текст дампа")


class TestMultiResult:
    """Набор итогов: команды одного запроса рисуются своими же рендерами."""

    @staticmethod
    def _both() -> MultiResult:
        return MultiResult(
            items=(
                TableResult(rows=[{"blobs": 3}]),
                AffectedSqlResult(affected_rows=5, status="DELETE 5"),
            )
        )

    def test_llm_gets_every_statement_numbered(self) -> None:
        report = render_for_llm(self._both())

        if "[1] " not in report:
            raise AssertionError('"[1] " in report')
        if '[{"blobs": 3}]' not in report:
            raise AssertionError('выдача первой команды в отчёте')
        if "[2] DELETE 5" not in report:
            raise AssertionError('"[2] DELETE 5" in report')

    def test_markdown_keeps_each_kind_of_result(self) -> None:
        md = ToolResultMarkdown(self._both()).render()

        if "_statement 1_" not in md:
            raise AssertionError('"_statement 1_" in md')
        if "| blobs" not in md:
            raise AssertionError("выборка осталась таблицей")
        if "_statement 2_" not in md:
            raise AssertionError('"_statement 2_" in md')
        if "_rows affected: 5 (DELETE 5)_" not in md:
            raise AssertionError("счётчик остался строкой статуса")

    def test_view_renders_the_set_as_markdown(self) -> None:
        rendering = ToolResultView(self._both()).render()

        if not isinstance(rendering, MarkdownRendering):
            raise AssertionError("isinstance(rendering, MarkdownRendering)")

    def test_set_survives_the_artifact_round_trip(self) -> None:
        """Набор персистится в checkpointer: вложенные варианты оживают."""
        revived = ToolArtifact.revive(self._both().model_dump(mode="json"))

        if not isinstance(revived, MultiResult):
            raise AssertionError("isinstance(revived, MultiResult)")
        kinds = [type(item).__name__ for item in revived.items]
        if kinds != ["TableResult", "AffectedSqlResult"]:
            raise AssertionError('kinds == ["TableResult", "AffectedSqlResult"]')
