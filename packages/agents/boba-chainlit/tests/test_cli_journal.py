"""Журнал прогона из терминала: адрес вызова ставит сам прогон.

Вне сессии чата контекст вызова ставить некому, поэтому его ставит
CliRunLauncher: пользователь `cli`, тред — метка запуска, call_id —
порядковый номер вызова прогона. Стадия здесь настоящая: bash в песочнице.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boba.chainlit.cli.ingest import CliRunLauncher
from boba.sandbox.caller import SandboxCaller
from boba.sandbox.journal import DirVault, StreamJournal
from boba.sandbox.runner import ToolCallContext
from boba.sandbox.workflow import StageDef, StageRegistry
from boba.stand.flow import SandboxMarks, StandSandbox
from boba.stand.shell import BashNodes
from boba.tool.shell.protocol import BashArgs, BashStage
from boba.toolkit.channels import Channel
from boba.toolkit.workflow import StageSpec, WorkflowSpec

TOOL = "ingest"
STAGE = "step"
HEAD_BYTES = 1 << 16


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Прогон идёт вне сессии чата: сессия и её контекст тесту не нужны."""


class AllowAllNodes:
    """Права вне сессии: в реестре прогона только его собственные узлы."""

    def __call__(self, tool: str, /) -> bool:
        return True


def _caller(journal: StreamJournal) -> SandboxCaller:
    sandbox = StandSandbox(packages=BashNodes.PACKAGES)

    definition = StageDef(
        contract=BashStage.CONTRACT,
        profile=sandbox.profile(),
        entry=BashStage.ENTRY,
        request=BashArgs,
        enrich=BashStage.enrich,
    )

    return SandboxCaller(
        StageRegistry({BashStage.NAME: definition}), AllowAllNodes(), dict, journal
    )


def _spec(command: str) -> WorkflowSpec:
    node = StageSpec(id=STAGE, tool=BashStage.NAME, args={"command": command})

    return WorkflowSpec(nodes=[node])


@SandboxMarks.NEEDS_SANDBOX
@SandboxMarks.NEEDS_USERNS
class TestCliJournal:
    """Прогон нумерует вызовы и кладёт их каналы в том секции [stream_journal]."""

    def test_calls_of_a_run_land_in_one_thread(self, tmp_path: Path) -> None:
        journal = StreamJournal(DirVault(str(tmp_path / "vault")), 0)
        mark = CliRunLauncher.run_mark(TOOL)
        runs = CliRunLauncher(_caller(journal), TOOL, mark)

        runs.call(_spec("echo первый"))
        runs.call(_spec("echo второй"))

        root = tmp_path / "vault" / CliRunLauncher.USER / mark

        first = root / f"call1.{STAGE}.tool_payload.log"
        second = root / f"call2.{STAGE}.tool_payload.log"

        assert first.read_bytes() == "первый\n".encode()
        assert second.read_bytes() == "второй\n".encode()

    def test_head_of_the_run_journal_is_readable(self, tmp_path: Path) -> None:
        journal = StreamJournal(DirVault(str(tmp_path / "vault")), 0)
        mark = CliRunLauncher.run_mark(TOOL)
        runs = CliRunLauncher(_caller(journal), TOOL, mark)

        outcome = runs.call(_spec("printf 'a\\nb\\n'"))

        key = outcome.journal_of(STAGE, Channel.TOOL_PAYLOAD)
        assert key is not None
        assert key.user_id == CliRunLauncher.USER
        assert key.thread_id == mark
        assert key.call_id == "call1"

        head = runs.head(key, HEAD_BYTES)

        assert head.text == "a\nb\n"
        assert head.truncated is False

    def test_the_run_leaves_no_context_behind(self, tmp_path: Path) -> None:
        """Контекст живёт ровно на время вызова: следующему хозяину он не мешает."""
        journal = StreamJournal(DirVault(str(tmp_path / "vault")), 0)
        runs = CliRunLauncher(_caller(journal), TOOL, CliRunLauncher.run_mark(TOOL))

        outer = ToolCallContext(
            user_id="7", thread_id="t-1", call_id="outer", tool="chat"
        )
        token = ToolCallContext.set(outer)
        try:
            runs.call(_spec("true"))

            assert ToolCallContext.current() == outer
        finally:
            ToolCallContext.reset(token)
