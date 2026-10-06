"""Запуск DAG насосов стенда без модели: описание toml → DagSpec → DagRunner.

Насосы postgres, ClickHouse и Oracle и узел dev_null собираются как в
приложении, общим стендом ToolStand: инструмент хоста, обёртка запуска
субпроцессом, соединения по имени из справочника стенда и обвязки цепочки
ToolChain. Описание — текст
toml; имена схем и баз стенда подставляются в него string.Template
($pg_schema, $ch_database и что ещё назвал тест), разбирает текст tomllib.

Ошибки:
StreamPlanError — план каналов описания нарушен.
DagRunError — узел зовёт инструмент, которого у стенда нет.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from string import Template
from typing import ClassVar

from boba.connection_broker.user_connections import ConnectionParamHooks
from boba.connections.manifest import ConnectionTypes
from boba.identity.run import Runs
from boba.stand.toolstand import ToolStand
from boba.stand_core.context import CallStand, StandIdentity
from boba.tool.ch import tools as ch
from boba.tool.ora import tools as ora
from boba.tool.pg import tools as pg
from boba.toolkit.chain import CallAmbient
from boba.toolkit.dag import DagSpec
from boba.toolrun.dag_run import DagOutcome
from boba.toolrun.hosted import DirectCalls, HostedTool
from boba.toolrun.injected import AsyncInjected, ParamSource
from boba.toolrun.stream_calls import StreamGroupsConfig
from boba.toolrun.streams import CallJournals

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


class PumpDags(ParamSource):
    """Платформа запуска DAG на стенде: насосы под обвязками приложения и
    исполнитель. Тест пишет описание toml и получает DagOutcome; имена
    стенда для описания — names, соединения по имени — connections.

    Инструменты собирает общий стенд ToolStand цепочкой обвязок ToolChain,
    как боевой загрузчик; сам объект — источник параметров-соединений этой
    цепочки (ParamSource): соединение узла берётся по имени из справочника
    стенда. Узлы исполняются в контексте вызова стенда: права цепочки
    проверяются по его роли и профилю.
    """

    CONNECTION_TEXT: ClassVar[str] = "Имя соединения из справочника стенда."

    ROLE: ClassVar[str] = "pump"
    THREAD: ClassVar[str] = "pump-dags"
    TIMEOUT_SEC: ClassVar[float] = 300.0

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
        self._directory = dict(connections)
        self._calls = CallStand()
        self._hooks = ConnectionParamHooks(
            ConnectionTypes.discover, self.CONNECTION_TEXT
        )

        contexts = self._calls.contexts
        stand = ToolStand(
            self.STREAM_CONFIG,
            contexts,
            CallJournals(None, Runs(contexts)),
            CallAmbient(),
            (self,),
        )
        stand.launch(
            (
                pg.pg_stream_out,
                pg.pg_stream_in,
                ch.ch_stream_out,
                ch.ch_stream_in,
                ora.ora_stream_out,
                ora.ora_stream_in,
            ),
            stand.process_launcher(self.THREAD, workdir, self.TIMEOUT_SEC),
        )

        registry = stand.registry(stand.access(self.ROLE, StandIdentity.PROFILE), ())
        by_name: dict[str, HostedTool] = {}
        for tool in registry.tools:
            by_name[tool.name] = tool

        self._runner = registry.runner(by_name, DirectCalls())

    def bind_all(self, tools: Sequence[HostedTool]) -> None:
        self._hooks.bind_all(tools, self._connection_hook)

    def _connection_hook(self, tool: str, param: str, kind: str) -> AsyncInjected:
        return StandConnections(param, self._directory)

    def spec(self, text: str) -> DagSpec:
        """Описание из toml-текста с подставленными именами стенда."""
        rendered = Template(text).substitute(self._names)

        return DagSpec.model_validate(tomllib.loads(rendered))

    async def run(self, text: str) -> DagOutcome:
        context = self._calls.context(self.THREAD, roles=(self.ROLE,))
        with self._calls.applied(context):
            return await self._runner.run(self.spec(text))
