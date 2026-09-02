"""Потоковая стадия workflow: задачи, связанные fd-ребром, живыми процессами.

Стенд: реестр из фейковых потоковых инструментов поверх субпроцессного
лончера — данные едут между задачами через ядро, как в конвейере чата.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from langchain_core.tools import StructuredTool
from psycopg import sql
from pydantic import SecretStr

from boba.access import ProfileGrant, RoleConfig, ToolAccess
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import CallContext
from boba.identity.locks import MemoryLiveLocks, RunLocking
from boba.messaging import MemoryMessageBus
from boba.stand.context import TEST_PROFILE, make_context
from boba.stand.fake_toolmod import FakeConfig, fake_echo, fake_stream
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import PayloadTool
from boba.toolkit.wrap import ToolProcessWrap
from boba.toolrun.intent import ToolIntentField
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller
from boba.toolrun.registry import ToolRegistry
from boba.workflow import RunStatus, TaskStatus
from boba.workflow_engine.service import WorkflowService
from boba.workflow_engine.store import WorkflowConfig, WorkflowStore

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "workflow_stream_test"
OWNER = UUID(int=9)
ROLE = "wf"
CFG = FakeConfig(token=SecretStr("t0ken"), limit=5)


def _registry(workdir: Path) -> ToolRegistry:
    launcher = ProcessToolCaller(
        "pipe",
        ProcessLauncherConfig.model_validate(
            {
                "provider": "process",
                "workdir": str(workdir),
                "shell": "/bin/bash",
                "timeout_sec": 60.0,
                "channel_limit_bytes": 8_000_000,
                "stderr_tail_bytes": 4096,
                "kill_grace_sec": 0.5,
            }
        ),
    )

    copies: list[PayloadTool] = []
    for tool in ToolMain.toolset(fake_stream, fake_echo):
        assert isinstance(tool, PayloadTool)
        copies.append(tool.model_copy())

    ToolProcessWrap.guard_all(copies, launcher)

    bridged: list[StructuredTool] = []
    for copy in copies:
        bridged.append(
            StructuredTool(
                name=copy.name,
                description=copy.description,
                args_schema=copy.args_schema,
                func=copy.func,
                coroutine=copy.coroutine,
                response_format=PayloadTool.RESPONSE_FORMAT,
            )
        )

    ToolIntentField.attach_all(list(bridged))

    names: list[str] = []
    for tool in bridged:
        names.append(tool.name)

    access = ToolAccess(
        tool_names=names,
        roles={ROLE: RoleConfig(tools=["*"])},
        profiles={TEST_PROFILE: ProfileGrant(tools=["*"], roles=["*"])},
    )
    return ToolRegistry(tools=list(bridged), access=access)


@pytest.fixture
async def service(
    pool: AsyncPostgresPool, tmp_path: Path
) -> WorkflowService:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    store = WorkflowStore(WorkflowConfig(enable=True, db_schema=SCHEMA), pool)
    await store.setup()

    registry = _registry(tmp_path)

    async def registry_ref() -> ToolRegistry:
        return registry

    return WorkflowService(
        store,
        registry_ref,
        "test:0",
        MemoryMessageBus("test:0"),
        RunLocking(locks=MemoryLiveLocks("test:0", 20), heartbeat_sec=1.0),
    )


def _context(monkeypatch: pytest.MonkeyPatch) -> CallContext:
    from boba.stand.context import install_context

    context = make_context(
        "wf-stream", user_id=OWNER, login="tester", roles=[ROLE]
    )
    install_context(monkeypatch, context)
    return context


def _spec(edges: str) -> str:
    cfg = json.dumps(CFG.revealed())
    return (
        "name: stream-stage\n"
        "tasks:\n"
        f"  produce: {{tool: fake_stream, args: {{prefix: 'a:', cfg: {cfg}}}}}\n"
        f"  consume: {{tool: fake_stream, args: {{prefix: 'b:', cfg: {cfg}}}}}\n"
        "edges:\n"
        f"{edges}"
    )


async def test_stream_edge_moves_data_between_tasks(
    service: WorkflowService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ребро produce.out -> consume.feed: кадры первого узла едут во второй
    через ядро, обе задачи закрываются done с конвертами."""
    context = _context(monkeypatch)

    stored = await service.save(
        context.subject, _spec('  - "produce.out -> consume.feed"\n'), {}
    )

    outcome = await service.run(context, stored, service.new_run_id())

    assert outcome.state.status is RunStatus.DONE, outcome.state
    assert outcome.state.tasks["produce"].status is TaskStatus.DONE
    assert outcome.state.tasks["consume"].status is TaskStatus.DONE
    assert "streamed" in outcome.results["produce"].llm_text()
    assert "streamed" in outcome.results["consume"].llm_text()


async def test_unknown_stream_port_is_refused_on_save(
    service: WorkflowService, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(monkeypatch)

    from boba.workflow_engine.service import WorkflowError

    with pytest.raises(WorkflowError, match="unknown port"):
        await service.save(
            context.subject, _spec('  - "produce.nope -> consume.feed"\n'), {}
        )
