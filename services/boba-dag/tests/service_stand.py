"""Сервер сервиса над фейками стенда: общий для тестов сервиса и клиента.

Тесты сервиса поднимают его в своём процессе; тесты клиента чата запускают
этот файл субпроцессом в окружении сервиса: `python service_stand.py <port>
<workdir>` — клиент живёт на других версиях библиотек и видит сервер только
по сети.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import uvicorn
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import SecretStr

from boba.access import ProfileGrant, RoleConfig, ToolAccess
from boba.dag_service.server import CallSchemas, DagServer, RunLimits
from boba.identity.context import CallContexts
from boba.identity.run import Runs
from boba.stand_core import fake_caller, fake_toolmod
from boba.stand_core.fake_toolmod import FakeConfig
from boba.toolkit.chain import CallAmbient
from boba.toolrun.bridge import ToolBridge
from boba.toolrun.callvalues import CallContextValues
from boba.toolrun.injected import InjectedConfig, StaticConfig
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller
from boba.toolrun.registry import ToolChain, ToolRegistry
from boba.toolrun.stream_calls import StreamGroupsConfig
from boba.toolrun.streams import CallJournals

PROFILE = "service"
DEV_TOKEN = "dev-token"
WEAK_TOKEN = "weak-token"


class ServiceStand:
    """Сервер сервиса над фейками стенда: реестр собран цепочкой обвязок,
    как его собирает загрузчик; роль dev видит всё, weak — один инструмент."""

    STREAM: StreamGroupsConfig = StreamGroupsConfig(
        open_sec=20.0,
        stall_sec=10.0,
        poll_sec=0.1,
        pipe_bytes=65536,
        pipe_bytes_max=1 << 30,
    )

    def __init__(self, workdir: Path, limits: RunLimits) -> None:
        self.schemas = CallSchemas()
        contexts = CallContexts()
        runs = Runs(contexts)
        ambient = CallAmbient()
        launcher = ProcessToolCaller(
            "dag-service",
            ProcessLauncherConfig(
                provider="process",
                workdir=str(workdir),
                timeout_sec=60.0,
                channel_limit_bytes=4_000_000,
                stderr_tail_bytes=8192,
                kill_grace_sec=0.5,
            ),
            contexts,
            ambient,
        )

        tools: list[Any] = []
        for payload in (
            fake_toolmod.fake_echo,
            fake_toolmod.fake_emit,
            fake_toolmod.fake_collect,
            fake_caller.fake_whoami,
            fake_caller.fake_sleep,
        ):
            tools.append(ToolBridge.as_structured_tool(payload.model_copy()))

        chain = ToolChain(
            self.STREAM,
            CallJournals(None, runs),
            contexts,
            ambient,
            (
                CallContextValues(contexts),
                InjectedConfig(self._config_of, StaticConfig()),
            ),
            (),
        )
        specs = chain.launch(tools, launcher)

        names: list[str] = []
        for tool in tools:
            names.append(tool.name)

        access = ToolAccess(
            tool_names=names,
            roles={
                "dev": RoleConfig(tools=["*"]),
                "weak": RoleConfig(tools=["fake_echo"]),
            },
            profiles={PROFILE: ProfileGrant(tools=["*"], roles=["*"])},
        )
        chain.seal(tools, access, specs)

        self.registry = ToolRegistry(
            tools=tools,
            access=access,
            stream_config=self.STREAM,
            own=frozenset(),
            node_args=(),
            specs=specs,
            ambient=ambient,
        )
        verifier = StaticTokenVerifier(
            tokens={
                DEV_TOKEN: self._claims("alice", "dev"),
                WEAK_TOKEN: self._claims("bob", "weak"),
            }
        )
        self.mcp = DagServer(self.registry, runs, verifier, PROFILE, limits).mcp()

    @staticmethod
    def _claims(login: str, role: str) -> dict[str, Any]:
        return {"client_id": "stand", "scopes": [], "login": login, "roles": [role]}

    @staticmethod
    def _config_of(name: str, annotation: object) -> object:
        return FakeConfig(token=SecretStr("t0ken"), limit=5)


if __name__ == "__main__":
    served = ServiceStand(Path(sys.argv[2]), RunLimits(max_runs=4, max_waiting=4))
    app = served.mcp.http_app(path="/mcp", stateless_http=True)
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
