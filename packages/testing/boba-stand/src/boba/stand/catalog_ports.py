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
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict

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
from boba.toolkit.ports import StreamSpecs
from boba.toolrun.dag_run import DagRunner
from boba.toolrun.hosted import DirectCalls
from boba.toolrun.injected import InjectedConfig, ToolConfigError
from boba.toolrun.invoke import ToolInvoker
from boba.toolrun.registry import ToolRegistry


class NoSyncTools(SyncTools):
    """Реализация SyncTools без инструментов."""

    async def invoker(self, subject: Subject) -> ToolInvoker:
        runner = DagRunner(
            {},
            {},
            StreamSpecs({}),
            CallAmbient(),
            StandRefs.STREAM_CONFIG.timings(),
            StandRefs.STREAM_CONFIG.pipe_bytes,
            DirectCalls(),
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

    CALL_TIMEOUT_SEC: ClassVar[float] = 60.0

    def __init__(self, stand: StandRefs) -> None:
        self._stand = stand
        self._runs = stand.runs

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
        catalog = site.catalog

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

        config = InjectedConfig(resolve, ServiceTickets(credentials))
        tools = self._stand.tool_stand((config,))
        tools.launch(
            [fake_pg_snapshot],
            tools.process_launcher("pipe", site.workdir, self.CALL_TIMEOUT_SEC),
        )

        return tools.registry(tools.access(site.role, site.profile), ())


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
