"""Итог вызова проходит формат MCP туда и обратно без потерь."""

from __future__ import annotations

import json
from typing import ClassVar

import mcp_types as mt
import pytest

from boba.toolkit.chain import GroupFailureResult
from boba.toolkit.dag import (
    DagNode,
    NestedCallsOutput,
    NodeOutcomes,
    NodeStatus,
    WorkflowNodeResult,
    WorkflowResult,
)
from boba.toolkit.mcp import McpResults
from boba.toolkit.result import (
    CanvasResult,
    ContentMime,
    ErrorResult,
    ExceptionResult,
    FailureCause,
    FileResult,
    MarkdownResult,
    ShellResult,
    SqlFailureResult,
    SqlResult,
    SqlStatement,
    TableResult,
    ToolResultBase,
    VisualResult,
)
from boba.toolkit.wire import McpContent, ResultWire, WireResult

WIRE = ResultWire()

OUTCOMES = NodeOutcomes()

CALL = DagNode(key="call_1", tool="probe")

SERVER = "probe-server"


def _for_model(wire: WireResult) -> str:
    """Текст, который из ответа получает модель клиента MCP."""
    reply = mt.CallToolResult(content=list(wire.content), is_error=wire.is_error)

    return McpResults().result(CALL.tool, CALL.key, reply).llm_view()


class TestRoundTrip:
    def test_kinds_of_other_modules_are_registered(self) -> None:
        """Виды, объявленные вне result.py, попадают в реестр при импорте."""
        if GroupFailureResult.declared_kind() is None:
            raise AssertionError("вид срыва группы объявлен в boba.toolkit.chain")
        if WorkflowResult.declared_kind() is None:
            raise AssertionError("вид workflow объявлен в boba.toolkit.dag")

    def test_traceback_stays_on_the_server(self) -> None:
        crashed = ExceptionResult(
            error_kind="ValueError",
            message="boom",
            traceback='Traceback (most recent call last):\n  File "/srv/x.py"',
        )

        wire = WIRE.packed(OUTCOMES.refused(CALL, crashed))

        for block in wire.content:
            if "/srv/x.py" in block.model_dump_json():
                raise AssertionError(f"трасса сервера клиенту не уходит: {block!r}")


class TestPlainBlocks:
    def test_result_without_an_output_schema_has_no_structured_content(
        self,
    ) -> None:
        table = TableResult(rows=[{"a": 1}])

        wire = WIRE.packed(OUTCOMES.of(CALL, table, False))

        if wire.structured is not None:
            raise AssertionError(f"structuredContent только по outputSchema: {wire}")


class TestNestedCallsWire:
    """Итог связки по 7.5: блоки узлов с заголовками по порядку и список
    узлов с номерами их блоков по outputSchema связки."""

    SECRET: ClassVar[str] = "/srv/boba/tools/ch.py"

    def _workflow(self) -> WorkflowResult:
        done = MarkdownResult(text="sent 3 rows into orders")
        crashed = ExceptionResult(
            error_kind="ClickHouseQueryError",
            message="table sink.orders does not exist",
            traceback=f'Traceback (most recent call last):\n  File "{self.SECRET}"',
        )
        nodes = [
            WorkflowNodeResult(
                key="src",
                call_id="call_1_0",
                tool="pg_stream_out",
                errored=False,
                content=done.llm_view(),
                result=done,
            ),
            WorkflowNodeResult(
                key="sink",
                call_id="call_1_1",
                tool="ch_stream_in",
                errored=False,
                content=crashed.llm_view(),
                result=crashed,
            ),
        ]

        return WorkflowResult(nodes=nodes, ok=False)

    def test_blocks_of_the_nodes_follow_their_headings(self) -> None:
        workflow = self._workflow()

        wire = WIRE.packed(OUTCOMES.of(CALL, workflow, True))

        texts: list[str] = []
        for block in wire.content:
            if not isinstance(block, mt.TextContent):
                raise AssertionError(f"блоки узлов — текст: {block!r}")

            if block.annotations is not None:
                continue

            texts.append(block.text)

        expected = [
            "[src] pg_stream_out — ok",
            "sent 3 rows into orders",
            "[sink] ch_stream_in — failed",
        ]
        if texts != expected:
            raise AssertionError(f"заголовок узла стоит перед его блоками: {texts}")

        if _for_model(wire) != workflow.llm_view():
            raise AssertionError("модель в своём процессе видит тот же текст")

    def test_nodes_point_at_their_blocks(self) -> None:
        workflow = self._workflow()

        wire = WIRE.packed(OUTCOMES.of(CALL, workflow, True))

        reply = NestedCallsOutput.model_validate(wire.structured)

        statuses: list[NodeStatus] = []
        for node, sent in zip(workflow.nodes, reply.nodes, strict=True):
            statuses.append(sent.status)
            if sent.call_id != node.call_id:
                raise AssertionError(f"узел назван вызовом узла: {sent!r}")

            pointed: list[mt.ContentBlock] = []
            for index in sent.blocks:
                pointed.append(wire.content[index])

            alone = WIRE.packed(OUTCOMES.of(CALL, node.result, False))
            if pointed != list(alone.content):
                raise AssertionError(f"номера ведут на блоки узла {node.key}: {sent}")

        if statuses != [NodeStatus.OK, NodeStatus.FAILED]:
            raise AssertionError(f"статус по результату узла: {statuses}")

    def test_trace_of_a_node_stays_on_the_server(self) -> None:
        wire = WIRE.packed(OUTCOMES.of(CALL, self._workflow(), True))

        if self.SECRET in json.dumps(wire.structured):
            raise AssertionError("трасса узла не уходит в structuredContent")

        for block in wire.content:
            if self.SECRET in block.model_dump_json():
                raise AssertionError(f"трасса узла не уходит в блоки: {block!r}")


class TestErrorFlag:
    def test_failure_result_is_an_error_even_with_success_status(self) -> None:
        """Тело вернуло отказ упакованным результатом: по MCP это сбой."""
        refusal = ErrorResult(message="no such table", error_kind="probe")

        wire = WIRE.packed(OUTCOMES.of(CALL, refusal, False))

        if not wire.is_error:
            raise AssertionError("отказ тела — сбой по MCP")

    def test_error_status_is_an_error(self) -> None:
        refusal = ErrorResult(message="bad arguments", error_kind="probe")

        wire = WIRE.packed(OUTCOMES.refused(CALL, refusal))

        if not wire.is_error:
            raise AssertionError("сорванный вызов — сбой по MCP")

    def test_non_zero_exit_is_an_error(self) -> None:
        shell = ShellResult(
            exit_code=2,
            stdout="",
            stdout_bytes=0,
            stdout_truncated=False,
            stderr="no such file",
            stderr_bytes=12,
            stderr_truncated=False,
            duration_ms=1,
            timed_out=False,
            ok=False,
        )

        wire = WIRE.packed(OUTCOMES.of(CALL, shell, False))

        if not wire.is_error:
            raise AssertionError("ненулевой код возврата — сбой по MCP")

    def test_plain_result_is_not_an_error(self) -> None:
        table = TableResult(rows=[{"a": 1}])

        wire = WIRE.packed(OUTCOMES.of(CALL, table, False))

        if wire.is_error:
            raise AssertionError("обычный результат — не сбой")


class TestContentBlocks:
    """content ответа по таблице 7.4: модель получает текст результата,
    человек — блоки по видам содержимого с audience user."""

    CRASH: ClassVar[ExceptionResult] = ExceptionResult(
        error_kind="ClickHouseQueryError",
        message="clickhouse statement failed: Code: 36. (BAD_ARGUMENTS)",
        causes=[FailureCause(error_type="DatabaseError", message="Code: 36.")],
        raised_at="boba/tool/ch/tools.py:812 in ch_stream_in",
        traceback="Traceback (most recent call last):\n  boom",
    )
    KINDS: ClassVar[tuple[ToolResultBase, ...]] = (
        CanvasResult(path="/workspace/t/mermaid/a.mmd", label="a.mmd", note="n"),
        ErrorResult(message="boom", error_kind="e"),
        CRASH,
        FileResult(
            path="/workspace/t/upload/a.pdf", name="a.pdf", mime="application/pdf"
        ),
        MarkdownResult(text="x", language="sql", note="n"),
        ShellResult(
            exit_code=0,
            stdout="hi",
            stdout_bytes=2,
            stdout_truncated=False,
            stderr="",
            stderr_bytes=0,
            stderr_truncated=False,
            duration_ms=1,
            timed_out=False,
        ),
        SqlResult(
            engine="postgres",
            statements=[SqlStatement(rows=[{"a": 1}], status="SELECT 1")],
        ),
        SqlFailureResult(
            error_kind="ClickHouseQueryError",
            cause=CRASH,
            columns=[{"column": "id", "type": "Nullable(Int32)"}],
            statements=[SqlStatement(text="create table t (id Nullable(Int32))")],
        ),
        GroupFailureResult(error_kind="stream_group_failed", cause=CRASH),
        TableResult(rows=[{"a": 1}], note="n"),
        VisualResult(element="card", props={"path": "/workspace/t/a.md"}, title="t"),
    )

    @pytest.mark.parametrize("artifact", KINDS, ids=lambda artifact: artifact.kind)
    def test_model_reads_the_text_of_the_result(self, artifact: ToolResultBase) -> None:
        wire = WIRE.packed(OUTCOMES.of(CALL, artifact, False))

        if _for_model(wire) != artifact.disclosed().llm_view():
            raise AssertionError(f"модель видит текст результата {artifact.kind}")

    def _user_blocks(self, artifact: ToolResultBase) -> list[mt.ContentBlock]:
        blocks: list[mt.ContentBlock] = []
        for block in WIRE.packed(OUTCOMES.of(CALL, artifact, False)).content:
            if block.annotations is None:
                blocks.append(block)
                continue

            if block.annotations.audience == ["user"]:
                blocks.append(block)

        return blocks

    def _resource(self, block: mt.ContentBlock) -> mt.TextResourceContents:
        if not isinstance(block, mt.EmbeddedResource):
            raise AssertionError(f"встроенный ресурс: {block!r}")

        if not isinstance(block.resource, mt.TextResourceContents):
            raise AssertionError(f"ресурс текстом: {block.resource!r}")

        return block.resource

    def test_plain_markdown_is_one_text_for_both(self) -> None:
        wire = WIRE.packed(OUTCOMES.of(CALL, MarkdownResult(text="hello"), False))

        if list(wire.content) != [mt.TextContent(text="hello")]:
            raise AssertionError(f"один текст без audience: {wire.content}")

    def test_markdown_with_a_language_is_a_resource_of_its_mime(self) -> None:
        shown = MarkdownResult(text='{"a": 1}\n', language="json", note="source x")

        blocks = self._user_blocks(shown)

        resource = self._resource(blocks[0])
        if resource.mime_type != ContentMime.JSON:
            raise AssertionError(f"MIME по языку: {resource.mime_type}")
        if resource.text != '{"a": 1}':
            raise AssertionError(f"текст без краевых переводов строк: {resource.text}")
        if not str(resource.uri).endswith("/json"):
            raise AssertionError(f"имя ресурса — язык: {resource.uri}")
        if blocks[1] != mt.TextContent(
            text="_source x_", annotations=mt.Annotations(audience=["user"])
        ):
            raise AssertionError(f"подпись — текстом человеку: {blocks[1]!r}")

    def test_table_is_csv(self) -> None:
        table = TableResult(rows=[{"a": 1, "b": None}, {"a": 2, "c": [1]}])

        blocks = self._user_blocks(table)

        resource = self._resource(blocks[0])
        if resource.mime_type != ContentMime.CSV:
            raise AssertionError(f"таблица — text/csv: {resource.mime_type}")
        if resource.text != "a,b,c\n1,,\n2,,[1]\n":
            raise AssertionError(f"строки таблицы в csv: {resource.text!r}")

    def test_sql_statement_is_its_text_and_rows(self) -> None:
        statement = SqlStatement(text="select 1 as a", rows=[{"a": 1}])
        result = SqlResult(engine="postgres", statements=[statement])

        blocks = self._user_blocks(result)

        mimes: list[str | None] = []
        for block in blocks:
            if isinstance(block, mt.EmbeddedResource):
                mimes.append(block.resource.mime_type)
                continue

            mimes.append(None)

        if mimes != [ContentMime.SQL, None, ContentMime.CSV]:
            raise AssertionError(f"команда, подпись, строки: {blocks}")

    def test_shell_output_is_plain_text_named_after_the_stream(self) -> None:
        shell = ShellResult(
            exit_code=1,
            stdout="",
            stdout_bytes=0,
            stdout_truncated=False,
            stderr="boom\n",
            stderr_bytes=5,
            stderr_truncated=False,
            duration_ms=3,
            timed_out=False,
        )

        blocks = self._user_blocks(shell)

        resource = self._resource(blocks[0])
        if resource.mime_type != ContentMime.PLAIN:
            raise AssertionError(f"вывод — text/plain: {resource.mime_type}")
        if not str(resource.uri).endswith("/stderr"):
            raise AssertionError(f"имя ресурса — поток: {resource.uri}")
        if blocks[1] != mt.TextContent(
            text="_exit code: 1_", annotations=mt.Annotations(audience=["user"])
        ):
            raise AssertionError(f"код возврата строкой: {blocks[1]!r}")

    def test_stored_plotly_visual_shows_only_its_caption(self) -> None:
        """Результат plotly из истории до удаления вида графика: модель его
        читает, а выдача пользователю — подпись без ресурса фигуры."""
        stored = {
            "kind": "visual",
            "element": "plotly",
            "props": {"spec": {"data": [{"type": "bar", "y": [1, 2]}]}},
            "title": "orders",
        }
        visual = VisualResult.model_validate(stored)

        blocks = self._user_blocks(visual)

        if len(blocks) != 1:
            raise AssertionError(f"только подпись: {blocks!r}")
        if blocks[0].type != "text":
            raise AssertionError(f"подпись текстом: {blocks[0]!r}")

    def test_file_and_canvas_are_links_under_one_text(self) -> None:
        attached = FileResult(
            path="/workspace/t/upload/a.pdf", name="a.pdf", mime="x/y"
        )
        shown = CanvasResult(path="/workspace/t/mermaid/a.mmd", label="a.mmd")

        for result, mime in ((attached, "x/y"), (shown, ContentMime.MERMAID)):
            wire = WIRE.packed(OUTCOMES.of(CALL, result, False))
            text, link = wire.content
            if text != mt.TextContent(text=result.llm_view()):
                raise AssertionError(f"текст модели и человека один: {text!r}")
            if not isinstance(link, mt.ResourceLink):
                raise AssertionError(f"файл — ссылкой: {link!r}")
            if link.uri != f"file://{result.path}":
                raise AssertionError(f"адрес — путь в workspace: {link.uri}")
            if link.mime_type != mime:
                raise AssertionError(f"MIME файла: {link.mime_type}")

    def test_only_the_canvas_asks_to_show_its_file(self) -> None:
        """Панель открывает ссылка с priority показа: её ставит canvas, а не
        вложение файла."""
        attached = FileResult(
            path="/workspace/t/upload/a.mmd", name="a.mmd", mime=ContentMime.MERMAID
        )
        shown = CanvasResult(path="/workspace/t/mermaid/a.mmd", label="a.mmd")

        expected = ((attached, None), (shown, McpContent.SHOWN_PRIORITY))
        for result, priority in expected:
            wire = WIRE.packed(OUTCOMES.of(CALL, result, False))
            link = wire.content[-1]
            if not isinstance(link, mt.ResourceLink):
                raise AssertionError(f"файл — ссылкой: {link!r}")
            if link.annotations is None:
                raise AssertionError("ссылка адресована человеку")
            if link.annotations.priority != priority:
                raise AssertionError(f"{result.kind}: {link.annotations.priority}")
            if link.annotations.audience != ["user"]:
                raise AssertionError(f"ссылка — человеку: {link.annotations}")

    def test_elapsed_rides_in_the_meta(self) -> None:
        table = TableResult(rows=[], elapsed_ms=42)

        wire = WIRE.packed(OUTCOMES.of(CALL, table, False))

        if wire.meta != {ResultWire.ELAPSED: 42}:
            raise AssertionError(f"длительность вызова в _meta: {wire.meta}")
