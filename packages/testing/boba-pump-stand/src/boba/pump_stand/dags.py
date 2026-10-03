"""Запуск DAG насосов стенда без модели: описание toml → DagSpec → DagRunner.

Насосы postgres, ClickHouse и Oracle и узел dev_null собираются как в
приложении: мост в langchain, обёртка запуска субпроцессом, соединения по
имени из справочника стенда, поля каналов, упаковка ошибок. Описание — текст
toml; имена схем и баз стенда подставляются в него string.Template
($pg_schema, $ch_database и что ещё назвал тест), разбирает текст tomllib.

Ошибки:
StreamPlanError — план каналов описания нарушен.
DagRunError — узел зовёт инструмент, которого у стенда нет.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from string import Template
from typing import ClassVar

from langchain_core.tools import BaseTool, StructuredTool

from boba.connection_broker.user_connections import ConnectionParamHooks
from boba.connections.manifest import ConnectionTypes
from boba.identity.context import CallContexts
from boba.tool.ch import tools as ch
from boba.tool.ora import tools as ora
from boba.tool.pg import tools as pg
from boba.toolkit.chain import CallAmbient
from boba.toolkit.dag import DagSpec
from boba.toolkit.wrap import ToolProcessWrap
from boba.toolrun.bridge import ToolBridge
from boba.toolrun.call_id import CallFields
from boba.toolrun.dag_run import DagOutcome, DagRunner
from boba.toolrun.dev_null import DevNullTool
from boba.toolrun.errors import ToolErrorGuard
from boba.toolrun.injected import AsyncInjected
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller
from boba.toolrun.stream_calls import StreamChannelFields, StreamGroupsConfig

__all__ = ["PumpDags", "StandConnections"]


class StandConnections(AsyncInjected):
    """Обвязка параметра-соединения стенда: имя из аргументов узла → профиль
    из справочника стенда. Ставит её PumpDags через ConnectionParamHooks —
    тем же порядком, что приложение ставит свои обвязки соединений."""

    def __init__(self, param: str, directory: Mapping[str, object]) -> None:
        super().__init__(param, None)
        self._directory = dict(directory)

    async def value(self, name: str, kwargs: dict[str, object]) -> object:
        given = kwargs.get(self.param)
        if not isinstance(given, str):
            msg = (
                f"tool {name!r}: {self.param!r} expects a connection name, "
                f"got {given!r}"
            )
            raise AssertionError(msg)

        config = self._directory.get(given)
        if config is None:
            msg = (
                f"tool {name!r}: the stand has no connection {given!r}; "
                f"known: {sorted(self._directory)}"
            )
            raise AssertionError(msg)

        return config


class PumpDags:
    """Платформа запуска DAG на стенде: насосы под обвязками приложения и
    исполнитель. Тест пишет описание toml и получает DagOutcome; имена
    стенда для описания — names, соединения по имени — connections."""

    CONNECTION_TEXT: ClassVar[str] = "Имя соединения из справочника стенда."

    STREAM_CONFIG: ClassVar[StreamGroupsConfig] = StreamGroupsConfig(
        open_sec=60.0,
        stall_sec=120.0,
        poll_sec=0.2,
        pipe_bytes=65536,
        pipe_bytes_max=1 << 30,
    )

    def __init__(
        self,
        workdir: Path,
        connections: Mapping[str, object],
        names: Mapping[str, str],
    ) -> None:
        self._names = dict(names)
        ambient = CallAmbient()

        launcher = ProcessToolCaller(
            "pump-dags",
            ProcessLauncherConfig(
                provider="process",
                workdir=str(workdir),
                timeout_sec=300.0,
                channel_limit_bytes=8_000_000,
                stderr_tail_bytes=16384,
                kill_grace_sec=1.0,
            ),
            CallContexts(),
            ambient,
        )

        tools: list[StructuredTool] = []
        for payload in (
            pg.pg_stream_out,
            pg.pg_stream_in,
            ch.ch_stream_out,
            ch.ch_stream_in,
            ora.ora_stream_out,
            ora.ora_stream_in,
        ):
            bridged = ToolBridge.as_structured_tool(payload.model_copy())
            if not isinstance(bridged, StructuredTool):
                msg = f"pump {payload.name!r} did not bridge to a StructuredTool"
                raise TypeError(msg)

            tools.append(bridged)

        drain_tool = DevNullTool(ambient)
        specs = ToolProcessWrap(ambient).guard_all(tools, launcher)
        specs = specs.declaring(DevNullTool.NAME, drain_tool.spec())
        self._directory = dict(connections)
        hooks = ConnectionParamHooks(ConnectionTypes.discover, self.CONNECTION_TEXT)
        hooks.bind_all(tools, self._connection_hook)

        drain = ToolBridge.as_structured_tool(drain_tool.build())
        every: list[BaseTool] = [*tools, drain]
        StreamChannelFields(self.STREAM_CONFIG).attach_all(every, specs)
        CallFields().attach_all(every)
        ToolErrorGuard().guard_all(every)

        by_name: dict[str, BaseTool] = {}
        for tool in every:
            by_name[tool.name] = tool

        self._runner = DagRunner(
            by_name,
            specs,
            ambient,
            self.STREAM_CONFIG.timings(),
            self.STREAM_CONFIG.pipe_bytes,
        )

    def _connection_hook(self, tool: str, param: str, kind: str) -> AsyncInjected:
        return StandConnections(param, self._directory)

    def spec(self, text: str) -> DagSpec:
        """Описание из toml-текста с подставленными именами стенда."""
        rendered = Template(text).substitute(self._names)

        return DagSpec.model_validate(tomllib.loads(rendered))

    async def run(self, text: str) -> DagOutcome:
        return await self._runner.run(self.spec(text))
