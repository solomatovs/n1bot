"""Узел bash в настоящей песочнице: обвязка, фасад и цепочка через ребро."""

from __future__ import annotations

from typing import Any

from boba.sandbox.caller import SandboxCaller
from boba.sandbox.runner import ToolCallContext
from boba.sandbox.workflow import StageDef, StageRegistry
from boba.stand.flow import SandboxMarks, StandSandbox
from boba.stand.journal import CallStand
from boba.stand.shell import BashNodes
from boba.tool.shell.protocol import BashArgs, BashStage
from boba.tool.shell.tools import BashRun
from boba.toolkit.channels import Channel
from boba.toolkit.launcher import TextCollector
from boba.toolkit.result import JsonResult, ToolResult
from boba.toolkit.workflow import EdgeSpec, StageSpec, WorkflowSpec

MAX_HEAD = 1 << 16


def _allow_all(tool: str, /) -> bool:
    return True


def _caller() -> SandboxCaller:
    sandbox = StandSandbox(packages=BashNodes.PACKAGES)

    definition = StageDef(
        contract=BashStage.CONTRACT,
        profile=sandbox.profile(),
        entry=BashStage.ENTRY,
        request=BashArgs,
        enrich=BashStage.enrich,
    )

    return SandboxCaller(
        StageRegistry({BashStage.NAME: definition}),
        _allow_all,
        dict,
        CallStand.journal(),
    )


def _payload(result: ToolResult) -> dict[str, Any]:
    assert isinstance(result, JsonResult)
    assert isinstance(result.payload, dict)
    return result.payload


@SandboxMarks.NEEDS_SANDBOX
@SandboxMarks.NEEDS_USERNS
class TestBashFacade:
    """Фасад строит граф из одного узла и собирает ответ из каналов стадии."""

    def test_stdout_travels_through_payload_channel(self) -> None:
        run = BashRun(_caller(), MAX_HEAD)

        result = run.run("echo привет", "")

        payload = _payload(result)
        assert result.ok
        assert payload["stdout"] == "привет\n"
        assert payload["exit_code"] == 0
        assert payload["truncated_stdout"] is False

    def test_stdin_literal_feeds_the_command(self) -> None:
        run = BashRun(_caller(), MAX_HEAD)

        result = run.run("cat", "строка входа\n")

        assert _payload(result)["stdout"] == "строка входа\n"

    def test_failed_command_keeps_its_output(self) -> None:
        run = BashRun(_caller(), MAX_HEAD)

        result = run.run("echo половина; exit 3", "")

        payload = _payload(result)
        assert result.ok is False
        assert payload["stdout"] == "половина\n"
        assert "3" in payload["message"]

    def test_head_is_truncated_by_the_ceiling(self) -> None:
        run = BashRun(_caller(), 16)

        result = run.run("printf 'x%.0s' $(seq 1 4096)", "")

        payload = _payload(result)
        assert payload["truncated_stdout"] is True
        assert payload["stdout"] == "x" * 16

    def test_the_ceiling_does_not_cut_the_journal(self) -> None:
        """Потолок — лимит чтения для модели; в файл канала пишется всё."""
        run = BashRun(_caller(), 16)

        run.run("printf 'x%.0s' $(seq 1 4096)", "")

        key = ToolCallContext.current().key(BashStage.NAME, Channel.TOOL_PAYLOAD)
        window = CallStand.journal().slice_at(key, 0)

        assert window is not None
        assert window.size == 4096

    def test_stderr_of_the_command_does_not_pollute_the_stream(self) -> None:
        run = BashRun(_caller(), MAX_HEAD)

        result = run.run("echo данные; echo шум >&2", "")

        assert _payload(result)["stdout"] == "данные\n"

    def test_stderr_reaches_the_model_from_its_own_journal(self) -> None:
        """Ответ собирается из голов двух каналов: продукта и tool_stderr."""
        run = BashRun(_caller(), MAX_HEAD)

        result = run.run("echo данные; echo шум >&2", "")

        payload = _payload(result)
        assert payload["stderr"] == "шум\n"
        assert payload["truncated_stderr"] is False

    def test_a_failed_command_reports_its_stderr(self) -> None:
        run = BashRun(_caller(), MAX_HEAD)

        result = run.run("echo причина >&2; exit 4", "")

        payload = _payload(result)
        assert result.ok is False
        assert payload["stderr"] == "причина\n"


@SandboxMarks.NEEDS_SANDBOX
@SandboxMarks.NEEDS_USERNS
class TestBashInGraph:
    """bash — узел-адаптер: его stdout течёт по ребру на stdin соседа."""

    def test_pipeline_of_two_stages(self) -> None:
        caller = _caller()
        spec = WorkflowSpec(
            nodes=(
                StageSpec(id="src", tool=BashStage.NAME, args={"command": "seq 1 5"}),
                StageSpec(
                    id="dst", tool=BashStage.NAME, args={"command": "grep -c ''"}
                ),
            ),
            edges=(EdgeSpec(src="src", dst="dst"),),
        )
        collector = TextCollector(max_chars=1000, limit_rows=None, header_lines=0)

        outcome = caller.call(spec, {"dst": collector})
        collector.close()

        assert collector.text() == "5\n"
        assert outcome.outcome_of("src").exit_code == 0
        assert outcome.outcome_of("dst").exit_code == 0

    def test_consumer_that_stops_early_is_not_a_failure(self) -> None:
        """`src | head`: источник получает EPIPE, граф остаётся успешным."""
        caller = _caller()
        spec = WorkflowSpec(
            nodes=(
                StageSpec(
                    id="src",
                    tool=BashStage.NAME,
                    args={"command": "seq 1 200000"},
                ),
                StageSpec(id="dst", tool=BashStage.NAME, args={"command": "head -1"}),
            ),
            edges=(EdgeSpec(src="src", dst="dst"),),
        )
        collector = TextCollector(max_chars=MAX_HEAD, limit_rows=None, header_lines=0)

        outcome = caller.call(spec, {"dst": collector})
        collector.close()

        assert collector.text() == "1\n"
        assert outcome.outcome_of("dst").exit_code == 0

    def test_consumer_that_never_reads_is_not_a_failure(self) -> None:
        """Вход подан узлу, которому он не нужен: лишние байты просто не читаются."""
        caller = _caller()
        spec = WorkflowSpec(
            nodes=(
                StageSpec(
                    id="src",
                    tool=BashStage.NAME,
                    args={"command": "seq 1 200000"},
                ),
                StageSpec(id="dst", tool=BashStage.NAME, args={"command": "echo ok"}),
            ),
            edges=(EdgeSpec(src="src", dst="dst"),),
        )
        collector = TextCollector(max_chars=MAX_HEAD, limit_rows=None, header_lines=0)

        outcome = caller.call(spec, {"dst": collector})
        collector.close()

        assert collector.text() == "ok\n"
        assert outcome.outcome_of("dst").exit_code == 0

