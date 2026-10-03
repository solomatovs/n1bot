"""Порты синхронизации каталога для стендов.

StubSyncPorts — стенд без синхронизаций: инструментов у субъекта нет,
подключения из заданной таблицы видны всем. FakeSyncPorts — стенд
синхронизации: реестр с фейком снятия fake_pg_snapshot поверх субпроцессного
лончера, как в приложении (домен каталога — injected-конфигом, kerberos
keytab — билетом вызова), и таблица подключений, видимых заданным
пользователям; id подключений выводятся из имён, как их выводит фейк.

Ошибки:
ToolConfigError — фейку просят подставить конфиг не того типа.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from uuid import UUID

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict

from boba.access import ProfileGrant, RoleConfig, ToolAccess
from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.catalog_service import (
    ConnectionDirectory,
    ConnectionInfo,
    RegistrySyncTools,
    SyncPorts,
    SyncSetupError,
    SyncTools,
)
from boba.connection_broker.tickets import ServiceTickets
from boba.connections.credentials import CredentialSource
from boba.db.postgres.catalog import CatalogStoreConfig
from boba.identity.context import Subject
from boba.stand.fake_sync import FakeConnection, fake_pg_snapshot
from boba.stand.refs import StandRefs
from boba.toolkit.chain import CallAmbient
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import PayloadTool
from boba.toolkit.ports import StreamSpecs
from boba.toolkit.wrap import ToolProcessWrap
from boba.toolrun.call_id import CallFields
from boba.toolrun.dag_run import DagRunner
from boba.toolrun.injected import InjectedConfig, ToolConfigError
from boba.toolrun.invoke import ToolInvoker
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller
from boba.toolrun.registry import ToolRegistry


class NoSyncTools(SyncTools):
    """Реализация SyncTools без инструментов."""

    async def invoker(self, subject: Subject) -> ToolInvoker:
        runner = DagRunner(
            {},
            StreamSpecs({}),
            CallAmbient(),
            StandRefs.STREAM_CONFIG.timings(),
            StandRefs.STREAM_CONFIG.pipe_bytes,
        )

        return ToolInvoker({}, runner)


class FakeConnections:
    """Подключения стенда по именам: id выводится из имени, как у фейка."""

    @staticmethod
    def info(name: str, kind: str) -> ConnectionInfo:
        return ConnectionInfo(id=FakeConnection.id_of(name), name=name, kind=kind)


class FakeSyncSite(BaseModel):
    """Где и от чьего имени живёт фейк снятия: рабочий каталог субпроцесса,
    роль и профиль доступа, домен каталога."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workdir: Path
    role: str
    profile: str
    catalog: CatalogStoreConfig


class CatalogPorts:
    """Порты каталога на стенде: без синхронизаций либо с фейком снятия.

    Создаётся тестом из стенда процесса (StandRefs): запуски открываются в
    его реестре, инструменты читают его держатель контекста вызова. stub()
    — инструментов нет, подключения из таблицы видны всем; fake() — фейк
    снятия субпроцессом и таблица подключений, видимых перечисленным
    пользователям; over() — без инструментов над своим каталогом
    подключений.
    """

    def __init__(self, stand: StandRefs) -> None:
        self._contexts = stand.contexts
        self._runs = stand.runs
        self._ambient = stand.ambient

    def stub(self, connections: Iterable[ConnectionInfo]) -> SyncPorts:
        return self.over(KnownConnectionDirectory(connections, None))

    def over(self, directory: ConnectionDirectory) -> SyncPorts:
        return SyncPorts(NoSyncTools(), directory, self._runs)

    def fake(
        self,
        site: FakeSyncSite,
        connections: Iterable[ConnectionInfo],
        visible_to: Iterable[UUID],
    ) -> SyncPorts:
        registry = self._registry(site)
        directory = KnownConnectionDirectory(connections, visible_to)

        async def registry_ref() -> ToolRegistry:
            return registry

        return SyncPorts(RegistrySyncTools(registry_ref), directory, self._runs)

    def _registry(self, site: FakeSyncSite) -> ToolRegistry:
        """Реестр инструментов с фейком снятия поверх субпроцессного лончера;
        домен каталога подставляется injected-конфигом, keytab его подключения
        едет билетом вызова."""
        workdir = site.workdir
        role = site.role
        profile = site.profile
        catalog = site.catalog
        launcher = ProcessToolCaller(
            "pipe",
            ProcessLauncherConfig.model_validate(
                {
                    "provider": "process",
                    "workdir": str(workdir),
                    "timeout_sec": 60.0,
                    "channel_limit_bytes": 8_000_000,
                    "stderr_tail_bytes": 4096,
                    "kill_grace_sec": 0.5,
                }
            ),
            self._contexts,
            self._ambient,
        )

        copies: list[PayloadTool] = []
        for tool in ToolMain.toolset(fake_pg_snapshot):
            if not isinstance(tool, PayloadTool):
                msg = f"fake sync tool {tool.name!r} is not a PayloadTool"
                raise TypeError(msg)

            copies.append(tool.model_copy())

        specs = ToolProcessWrap(self._ambient).guard_all(copies, launcher)

        def resolve(param: str, annotation: object) -> object:
            if annotation is CatalogStoreConfig:
                return catalog

            msg = (
                f"fake sync stand: injected parameter {param!r} wants "
                f"{annotation!r}, the stand only carries CatalogStoreConfig"
            )
            raise ToolConfigError(msg)

        def credentials() -> CredentialSource:
            return KerberosCredentialSource(None, NoRefresh())

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

        InjectedConfig(resolve, ServiceTickets(credentials)).bind_all(bridged)
        CallFields().attach_all(list(bridged))

        names: list[str] = []
        for tool in bridged:
            names.append(tool.name)

        access = ToolAccess(
            tool_names=names,
            roles={role: RoleConfig(tools=["*"])},
            profiles={profile: ProfileGrant(tools=["*"], roles=["*"])},
        )
        return ToolRegistry(
            tools=list(bridged),
            access=access,
            stream_config=StandRefs.STREAM_CONFIG,
            own=frozenset(),
            node_args=(),
            specs=specs,
            ambient=self._ambient,
        )


class KnownConnectionDirectory(ConnectionDirectory):
    """Реализация ConnectionDirectory таблицей подключений, видимых
    перечисленным пользователям; None — видны всем."""

    def __init__(
        self, connections: Iterable[ConnectionInfo], visible_to: Iterable[UUID] | None
    ) -> None:
        self._connections: dict[UUID, ConnectionInfo] = {}
        for connection in connections:
            self._connections[connection.id] = connection

        self._visible_to: frozenset[UUID] | None = None
        if visible_to is not None:
            self._visible_to = frozenset(visible_to)

    async def info_of(self, subject: Subject, connection_id: UUID) -> ConnectionInfo:
        found = self._connections.get(connection_id)
        if self._visible_to is not None and subject.user_id not in self._visible_to:
            found = None

        if found is not None:
            return found

        msg = f"connection {connection_id} is not visible to {subject.login!r}"
        raise SyncSetupError(msg)

    async def named(self, subject: Subject, name: str) -> ConnectionInfo:
        for connection in self._connections.values():
            if connection.name != name:
                continue

            return await self.info_of(subject, connection.id)

        msg = f"connection {name!r} is not visible to {subject.login!r}"
        raise SyncSetupError(msg)
