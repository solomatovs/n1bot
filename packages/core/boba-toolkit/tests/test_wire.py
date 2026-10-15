"""Итог вызова проходит формат MCP туда и обратно без потерь.

Каждый вид результата проверяется на снимках истории: тех же, по которым
проверяется оживление сохранённых сообщений.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import mcp_types as mt
import pytest

from boba.stand_core.history import HistorySnapshots
from boba.toolkit.chain import GroupFailureResult
from boba.toolkit.dag import (
    DagNode,
    JournalAddress,
    NodeOutcomes,
    NodeStatus,
    WorkflowNodeResult,
    WorkflowResult,
)
from boba.toolkit.result import (
    ErrorResult,
    ExceptionResult,
    FailureResult,
    MarkdownResult,
    TableResult,
    ToolArtifact,
)
from boba.toolkit.wire import (
    CallStatus,
    CallWire,
    NestedCallsReply,
    ResultWire,
    WirePart,
    WireResult,
)

SNAPSHOTS = HistorySnapshots(Path(__file__).parent / "history", "boba.toolkit")

WIRE = ResultWire()

OUTCOMES = NodeOutcomes()

CALL = DagNode(key="call_1", tool="probe")

SERVER = "probe-server"


def _snapshot(path: Path):
    """Результат семейства из снимка истории."""
    revived = ToolArtifact.revive(json.loads(path.read_text(encoding="utf-8")))
    if revived is None:
        raise AssertionError(f"снимок {path.name} оживает в результат семейства")

    return revived


class TestRoundTrip:
    def test_kinds_of_other_modules_are_registered(self) -> None:
        """Виды, объявленные вне result.py, попадают в реестр при импорте."""
        if GroupFailureResult.declared_kind() is None:
            raise AssertionError("вид срыва группы объявлен в boba.toolkit.chain")
        if WorkflowResult.declared_kind() is None:
            raise AssertionError("вид workflow объявлен в boba.toolkit.dag")

    @pytest.mark.parametrize("path", SNAPSHOTS.paths(), ids=lambda path: path.stem)
    def test_every_kind_survives_the_wire(self, path: Path) -> None:
        artifact = _snapshot(path)
        if artifact.structured() is not None:
            # вид со своей outputSchema едет её данными: TestNestedCallsWire
            return

        outcome = OUTCOMES.of(CALL, artifact, False)

        wire = WIRE.packed(outcome)
        sent = json.loads(json.dumps(wire.structured))
        revived = WIRE.revived(
            WireResult(content=wire.content, structured=sent, is_error=wire.is_error),
            CALL,
            SERVER,
        )

        # клиенту уходит результат без того, чему место в журнале сервера
        disclosed = artifact.disclosed()
        if revived is None:
            raise AssertionError(f"результат вида {path.stem} оживает итогом")
        if revived.artifact != disclosed:
            raise AssertionError(f"результат вида {path.stem} оживает тем же")
        if type(revived.artifact) is not type(artifact):
            raise AssertionError(f"класс результата тот же: {type(revived.artifact)}")
        if revived.content != artifact.llm_view():
            raise AssertionError("текст для модели доехал как есть")
        if revived != outcome.model_copy(update={"artifact": disclosed}):
            raise AssertionError(f"итог вызова доехал тем же: {revived!r}")

    def test_call_id_and_status_ride_in_the_structured_content(self) -> None:
        table = TableResult(rows=[{"a": 1}])

        wire = WIRE.packed(OUTCOMES.of(CALL, table, False))

        structured = wire.structured
        if structured is None:
            raise AssertionError("итог несёт structuredContent")

        call = CallWire.model_validate(structured[WirePart.CALL.value])
        if call != CallWire(id=CALL.key, status=CallStatus.SUCCESS):
            raise AssertionError(f"сведения о вызове доехали: {call!r}")
        if structured[WirePart.RESULT.value] != table.model_dump(mode="json"):
            raise AssertionError(f"результат лежит своей частью: {structured!r}")

    def test_run_and_call_of_the_server_become_the_journal_address(self) -> None:
        """Журнал сервер ведёт под своим идентификатором вызова: он и запуск
        дают адрес, а вызов клиента называется по-своему."""
        table = TableResult(rows=[{"a": 1}])
        served = DagNode(key="api-served", tool="probe")
        wire = WIRE.packed(OUTCOMES.of(served, table, False))
        structured = wire.structured
        if structured is None:
            raise AssertionError("итог несёт structuredContent")

        stamped = WireResult(
            content=wire.content,
            structured=WIRE.stamped(structured, "run-7"),
            is_error=wire.is_error,
        )

        revived = WIRE.revived(stamped, CALL, SERVER)

        if revived is None:
            raise AssertionError("результат оживает итогом")
        if revived.key != CALL.key:
            raise AssertionError(f"итог называется вызовом клиента: {revived.key}")

        expected = JournalAddress(server=SERVER, run="run-7", call="api-served")
        if revived.journal != expected:
            raise AssertionError(f"адрес журнала собран: {revived.journal!r}")

    def test_traceback_stays_on_the_server(self) -> None:
        crashed = ExceptionResult(
            error_kind="ValueError",
            message="boom",
            traceback='Traceback (most recent call last):\n  File "/srv/x.py"',
        )

        wire = WIRE.packed(OUTCOMES.refused(CALL, crashed))

        if "/srv/x.py" in json.dumps(wire.structured):
            raise AssertionError(f"трасса сервера клиенту не уходит: {wire!r}")

    def test_result_without_a_run_has_no_journal(self) -> None:
        table = TableResult(rows=[{"a": 1}])

        revived = WIRE.revived(
            WIRE.packed(OUTCOMES.of(CALL, table, False)), CALL, SERVER
        )

        if revived is None:
            raise AssertionError("результат оживает итогом")
        if revived.journal is not None:
            raise AssertionError(f"без запуска адреса журнала нет: {revived.journal}")


class TestPlainBlocks:
    def test_result_without_own_data_is_one_text_block_and_its_model(self) -> None:
        table = TableResult(rows=[{"a": 1}])

        wire = WIRE.packed(OUTCOMES.of(CALL, table, False))

        if list(wire.content) != [mt.TextContent(text=table.llm_view())]:
            raise AssertionError(f"один текстовый блок для модели: {wire.content}")

        structured = wire.structured
        if structured is None:
            raise AssertionError("итог несёт structuredContent")
        if sorted(structured) != sorted(WirePart):
            raise AssertionError(f"части итога — call и result: {sorted(structured)}")


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

            texts.append(block.text)

        expected = [
            "[src] pg_stream_out — ok",
            "sent 3 rows into orders",
            "[sink] ch_stream_in — failed",
            workflow.nodes[1].content,
        ]
        if texts != expected:
            raise AssertionError(f"заголовок узла стоит перед его блоками: {texts}")

        if "\n".join(texts) != workflow.llm_view():
            raise AssertionError("модель в своём процессе видит тот же текст")

    def test_nodes_point_at_their_blocks(self) -> None:
        workflow = self._workflow()

        wire = WIRE.packed(OUTCOMES.of(CALL, workflow, True))

        reply = NestedCallsReply.model_validate(wire.structured)
        if reply.call != CallWire(id=CALL.key, status=CallStatus.ERROR):
            raise AssertionError(f"сведения о вызове доехали: {reply.call!r}")

        statuses: list[NodeStatus] = []
        for node, sent in zip(workflow.nodes, reply.nodes, strict=True):
            statuses.append(sent.status)
            if sent.call_id != node.call_id:
                raise AssertionError(f"узел назван вызовом узла: {sent!r}")

            pointed: list[str] = []
            for index in sent.blocks:
                block = wire.content[index]
                if not isinstance(block, mt.TextContent):
                    raise AssertionError(f"блок узла {node.key} текстовый: {block!r}")

                pointed.append(block.text)

            if pointed != list(node.result.disclosed().texts()):
                raise AssertionError(f"номера ведут на блоки узла {node.key}: {sent}")

            if sent.result != node.result.disclosed().model_dump(mode="json"):
                raise AssertionError(f"модель узла доехала: {sent.result}")

        if statuses != [NodeStatus.OK, NodeStatus.FAILED]:
            raise AssertionError(f"статус по результату узла: {statuses}")

    def test_trace_of_a_node_stays_on_the_server(self) -> None:
        wire = WIRE.packed(OUTCOMES.of(CALL, self._workflow(), True))

        if self.SECRET in json.dumps(wire.structured):
            raise AssertionError("трасса узла не уходит в structuredContent")

        for block in wire.content:
            if self.SECRET in block.model_dump_json():
                raise AssertionError(f"трасса узла не уходит в блоки: {block!r}")

    def test_run_is_stamped_into_the_call(self) -> None:
        wire = WIRE.packed(OUTCOMES.of(CALL, self._workflow(), True))
        structured = wire.structured
        if structured is None:
            raise AssertionError("итог несёт structuredContent")

        reply = NestedCallsReply.model_validate(WIRE.stamped(structured, "run-7"))
        if reply.call.run != "run-7":
            raise AssertionError(f"запуск проставлен в сведения о вызове: {reply}")

    def test_client_without_the_model_gets_no_outcome(self) -> None:
        """Частей call/result у связки нет: клиент показывает её блоки."""
        wire = WIRE.packed(OUTCOMES.of(CALL, self._workflow(), True))

        if WIRE.revived(wire, CALL, SERVER) is not None:
            raise AssertionError("итог связки не оживает моделью вида")


class TestErrorFlag:
    def test_failure_result_is_an_error_even_with_success_status(self) -> None:
        """Тело вернуло отказ упакованным результатом: статус вызова success,
        а по MCP это сбой; клиент получает статус как был."""
        refusal = ErrorResult(message="no such table", error_kind="probe")

        wire = WIRE.packed(OUTCOMES.of(CALL, refusal, False))

        if not wire.is_error:
            raise AssertionError("отказ тела — сбой по MCP")

        revived = WIRE.revived(wire, CALL, SERVER)
        if revived is None:
            raise AssertionError("отказ оживает итогом")
        if revived.errored:
            raise AssertionError(f"статус вызова восстановлен: {revived!r}")
        if not isinstance(revived.artifact, FailureResult):
            raise AssertionError(f"отказ оживает отказом: {revived.artifact!r}")

    def test_error_status_is_an_error(self) -> None:
        refusal = ErrorResult(message="bad arguments", error_kind="probe")

        wire = WIRE.packed(OUTCOMES.refused(CALL, refusal))

        if not wire.is_error:
            raise AssertionError("сорванный вызов — сбой по MCP")

        revived = WIRE.revived(wire, CALL, SERVER)
        if revived is None or not revived.errored:
            raise AssertionError(f"статус error восстановлен: {revived!r}")

    def test_plain_result_is_not_an_error(self) -> None:
        table = TableResult(rows=[{"a": 1}])

        wire = WIRE.packed(OUTCOMES.of(CALL, table, False))

        if wire.is_error:
            raise AssertionError("обычный результат — не сбой")


class TestForeignServer:
    """Сервер с чужим содержимым: клиент показывает текст."""

    def test_unknown_kind_falls_back_to_the_text(self) -> None:
        call = CallWire(id="api-1", status=CallStatus.SUCCESS)
        wire = WireResult(
            content=[mt.TextContent(text="plain text")],
            structured={
                WirePart.CALL.value: call.model_dump(mode="json"),
                WirePart.RESULT.value: {"kind": "kind_of_a_newer_server", "x": 1},
            },
            is_error=False,
        )

        if WIRE.revived(wire, CALL, SERVER) is not None:
            raise AssertionError("неизвестный вид не оживает итогом")

    def test_result_without_structured_content_is_not_revived(self) -> None:
        wire = WireResult(
            content=[mt.TextContent(text="boom")], structured=None, is_error=True
        )

        if WIRE.revived(wire, CALL, SERVER) is not None:
            raise AssertionError("без structuredContent итога семейства нет")

    def test_structured_content_of_another_shape_is_not_revived(self) -> None:
        """Чужой сервер прислал свои данные: частей итога в них нет."""
        table = TableResult(rows=[{"a": 1}])
        wire = WireResult(
            content=[mt.TextContent(text=table.llm_view())],
            structured=table.model_dump(mode="json"),
            is_error=False,
        )

        if WIRE.revived(wire, CALL, SERVER) is not None:
            raise AssertionError("данные без сведений о вызове не оживают итогом")
