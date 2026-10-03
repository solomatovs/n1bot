"""REST-запуск DAG человеком: тот же исполнитель, что у чата, под контекстом api.

Стенд: фейковые потоковые инструменты под обвязками реестра, тела —
настоящие субпроцессы; пользователь и профиль — из стенда studio.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from studio_stand import StandProfiles

from boba.access import ProfileGrant, RoleConfig, ToolAccess
from boba.identity.context import CallContexts
from boba.identity.locks import MemoryLiveLocks
from boba.identity.run import Runs
from boba.runtime.config import StudioRuntimeConfig
from boba.stand.tools import STREAM_CONFIG
from boba.stand_core import fake_toolmod
from boba.stand_core.fake_toolmod import FakeConfig
from boba.studio.api.dags import DagRunBody, DagRunning, DagRunReply
from boba.toolkit.chain import StreamFailureKind
from boba.toolkit.wrap import ToolProcessWrap
from boba.toolrun.bridge import ToolBridge
from boba.toolrun.call_id import ToolCallIdField
from boba.toolrun.errors import ToolErrorGuard
from boba.toolrun.injected import InjectedConfig
from boba.toolrun.intent import ToolIntentField
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.run_log import ToolRunLogger
from boba.toolrun.stream_calls import StreamChannelFields
from boba.toolrun.streams import CallJournals

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CFG = FakeConfig(token=SecretStr("t0ken"), limit=5)


class FakeStreamTools:
    """Источник и приёмник стенда под обвязками реестра, как их ставит
    загрузчик: обёртка запуска, конфиг, поля каналов, id и intent вызова,
    журнал, упаковка ошибок."""

    def __init__(self, workdir: Path) -> None:
        self.contexts = CallContexts()
        self.runs = Runs(self.contexts)
        self.journals = CallJournals(None, self.runs)
        launcher = ProcessToolCaller(
            "dag-api",
            ProcessLauncherConfig(
                provider="process",
                workdir=str(workdir),
                timeout_sec=60.0,
                channel_limit_bytes=4_000_000,
                stderr_tail_bytes=8192,
                kill_grace_sec=0.5,
            ),
            self.contexts,
        )

        tools: list[Any] = []
        for payload in (fake_toolmod.fake_emit, fake_toolmod.fake_collect):
            tools.append(ToolBridge.as_structured_tool(payload.model_copy()))

        ToolProcessWrap.guard_all(tools, launcher)
        InjectedConfig.bind_all(tools, self._config_of)
        StreamChannelFields(STREAM_CONFIG).attach_all(tools)
        ToolCallIdField.attach_all(tools)
        ToolIntentField.attach_all(tools)
        ToolRunLogger(self.journals, self.contexts).guard_all(tools)
        ToolErrorGuard().guard_all(tools)
        self.tools = tools

    @staticmethod
    def _config_of(name: str, annotation: object) -> object:
        return CFG

    def registry(self, config: StudioRuntimeConfig) -> ToolRegistry:
        names: list[str] = []
        for tool in self.tools:
            names.append(tool.name)

        roles: dict[str, RoleConfig] = {}
        for role in StandProfiles.roles(config):
            roles[role] = RoleConfig(tools=["*"])

        access = ToolAccess(
            tool_names=names,
            roles=roles,
            profiles={
                StandProfiles.profile(config): ProfileGrant(tools=["*"], roles=["*"])
            },
        )
        return ToolRegistry(
            tools=self.tools,
            access=access,
            stream_config=STREAM_CONFIG,
            own=frozenset(),
            node_args=(),
        )


def _running(stand: FakeStreamTools, config: StudioRuntimeConfig) -> DagRunning:
    async def registry() -> ToolRegistry:
        return stand.registry(config)

    return DagRunning(
        registry,
        StandProfiles.profiles(config),
        lambda: MemoryLiveLocks("test:0", 20),
        1.0,
        stand.runs,
    )


def _emit(key: str, channel: str, *, fail: bool = False) -> dict[str, Any]:
    return {
        "key": key,
        "tool": "fake_emit",
        "args": {
            "prefix": "a",
            "count": 4,
            "size": 0,
            "fail_midway": fail,
            "out": channel,
        },
    }


def _collect(key: str, channel: str, marker: Path) -> dict[str, Any]:
    """Приёмник пишет файл фиксации по абсолютному пути: cwd процесса —
    workspace запуска, а не каталог стенда."""
    return {
        "key": key,
        "tool": "fake_collect",
        "args": {"marker": str(marker), "fail": False, "gated": True, "feed": channel},
    }


def _body(config: StudioRuntimeConfig, *nodes: dict[str, Any]) -> DagRunBody:
    return DagRunBody.model_validate(
        {
            "profile": StandProfiles.profile(config),
            "intent": "run the dag",
            "dag": {"name": "api", "version": 1, "nodes": list(nodes)},
        }
    )


async def _serve(
    tmp_path: Path, config: StudioRuntimeConfig, *nodes: dict[str, Any]
) -> DagRunReply:
    stand = FakeStreamTools(tmp_path)
    user = StandProfiles.user(config)

    return await _running(stand, config).serve(_body(config, *nodes), user)


class TestServe:
    async def test_dag_runs_every_node_and_commits(
        self, tmp_path: Path, studio_config: StudioRuntimeConfig
    ) -> None:
        reply = await _serve(
            tmp_path,
            studio_config,
            _emit("src", "rows"),
            _collect("sink", "rows", tmp_path / "done"),
        )

        assert reply.ok, reply
        assert reply.dag == "api"
        assert [node.key for node in reply.nodes] == ["src", "sink"]
        assert reply.nodes[1].content.startswith("collected 4 frames")
        assert (tmp_path / "done").read_text().startswith("collected 4 frames")

    async def test_broken_plan_is_unprocessable(
        self, tmp_path: Path, studio_config: StudioRuntimeConfig
    ) -> None:
        with pytest.raises(HTTPException) as caught:
            await _serve(
                tmp_path,
                studio_config,
                _emit("a", "rows"),
                _emit("b", "rows"),
                _collect("sink", "rows", tmp_path / "never"),
            )

        assert caught.value.status_code == 422
        assert "two writers" in str(caught.value.detail)
        assert not (tmp_path / "never").exists()

    async def test_unknown_tool_is_not_found(
        self, tmp_path: Path, studio_config: StudioRuntimeConfig
    ) -> None:
        ghost = {"key": "g", "tool": "ghost", "args": {}}

        with pytest.raises(HTTPException) as caught:
            await _serve(tmp_path, studio_config, ghost)

        assert caught.value.status_code == 404

    async def test_failed_writer_fails_every_node(
        self, tmp_path: Path, studio_config: StudioRuntimeConfig
    ) -> None:
        reply = await _serve(
            tmp_path,
            studio_config,
            _emit("src", "rows", fail=True),
            _collect("sink", "rows", tmp_path / "cut"),
        )

        assert not reply.ok
        for node in reply.nodes:
            assert not node.ok, node
            assert node.result["error_kind"] == StreamFailureKind.GROUP_FAILED

        assert not (tmp_path / "cut").exists()
