"""Порты синхронизации каталога для стендов.

StubSyncPorts — стенд без синхронизаций: инструментов у субъекта нет,
подключения не видны. FakeSyncPorts — стенд синхронизации: реестр с фейком
снятия fake_pg_snapshot поверх субпроцессного лончера, как в приложении, и
таблица имён подключений, видимых заданным пользователям.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import ClassVar
from uuid import UUID

from langchain_core.tools import StructuredTool

from boba.access import ProfileGrant, RoleConfig, ToolAccess
from boba.catalog_service import (
    ConnectionDirectory,
    ConnectionEntry,
    SyncPorts,
    SyncSetupError,
    SyncTools,
)
from boba.identity.context import Subject
from boba.stand.fake_sync import fake_pg_snapshot
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import PayloadTool
from boba.toolkit.wrap import ToolProcessWrap
from boba.toolrun.intent import ToolIntentField
from boba.toolrun.invoke import ToolInvoker
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller
from boba.toolrun.registry import ToolRegistry


class NoSyncTools(SyncTools):
    """Реализация SyncTools без инструментов."""

    async def invoker(self, subject: Subject) -> ToolInvoker:
        return ToolInvoker({})


class NoConnectionDirectory(ConnectionDirectory):
    """Реализация ConnectionDirectory, которой не видно ни одного подключения."""

    async def visible(self, subject: Subject, kind: str) -> Sequence[ConnectionEntry]:
        return ()

    async def name_of(self, subject: Subject, connection_id: UUID) -> str:
        msg = f"connection {connection_id} is not visible to {subject.login!r}"
        raise SyncSetupError(msg)


class StubSyncPorts(SyncPorts):
    """Порты стенда без синхронизаций."""

    def __init__(self) -> None:
        super().__init__(NoSyncTools(), NoConnectionDirectory())


class FakeSyncRegistry:
    """Реестр инструментов с фейком снятия поверх субпроцессного лончера."""

    @classmethod
    def build(cls, workdir: Path, role: str, profile: str) -> ToolRegistry:
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
        for tool in ToolMain.toolset(fake_pg_snapshot):
            if not isinstance(tool, PayloadTool):
                msg = f"fake sync tool {tool.name!r} is not a PayloadTool"
                raise TypeError(msg)

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
            roles={role: RoleConfig(tools=["*"])},
            profiles={profile: ProfileGrant(tools=["*"], roles=["*"])},
        )
        return ToolRegistry(tools=list(bridged), access=access)


class RegistrySyncTools(SyncTools):
    """Реализация SyncTools реестром стенда."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def invoker(self, subject: Subject) -> ToolInvoker:
        return ToolInvoker(self._registry.for_headless(subject.roles, subject.profile))


class KnownConnectionDirectory(ConnectionDirectory):
    """Реализация ConnectionDirectory таблицей: имена по id, видимые
    перечисленным пользователям; все строки — вида KIND."""

    KIND: ClassVar[str] = "postgres"

    def __init__(self, names: Mapping[UUID, str], visible_to: Iterable[UUID]) -> None:
        self._names = dict(names)
        self._visible_to = frozenset(visible_to)

    async def visible(self, subject: Subject, kind: str) -> Sequence[ConnectionEntry]:
        if subject.user_id not in self._visible_to:
            return ()

        if kind != self.KIND:
            return ()

        entries: list[ConnectionEntry] = []
        for connection_id, name in self._names.items():
            entries.append(
                ConnectionEntry(id=connection_id, name=name, kind=kind, mine=True)
            )

        return entries

    async def name_of(self, subject: Subject, connection_id: UUID) -> str:
        name = self._names.get(connection_id)
        if subject.user_id not in self._visible_to:
            name = None

        if name is not None:
            return name

        msg = f"connection {connection_id} is not visible to {subject.login!r}"
        raise SyncSetupError(msg)


class FakeSyncPorts(SyncPorts):
    """Порты стенда синхронизации: фейк снятия и таблица подключений."""

    def __init__(
        self,
        workdir: Path,
        role: str,
        profile: str,
        names: Mapping[UUID, str],
        visible_to: Iterable[UUID],
    ) -> None:
        registry = FakeSyncRegistry.build(workdir, role, profile)
        super().__init__(
            RegistrySyncTools(registry), KnownConnectionDirectory(names, visible_to)
        )
