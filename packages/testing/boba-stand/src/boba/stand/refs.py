"""Собирает зависимости тестового загрузчика плагинов."""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from omegaconf import DictConfig

from boba.access import GrantCheck, ProfileGrant, ToolGrant, ToolGrants
from boba.connections.credentials import CredentialSource
from boba.connections.manifest import ConnectionTypes
from boba.connections.stored import ConnectionRepository
from boba.identity.context import CallContexts
from boba.identity.run import Runs
from boba.krb.connections import KerberosCredentialSource
from boba.mcp_server.connections import NoRefresh
from boba.stand.connections import DisabledConnectionRepository, StandUserConnections
from boba.stand.toolstand import ToolStand
from boba.tool_io.chain import CallAmbient
from boba.toolrun.hosted import HostedTool
from boba.toolrun.injected import InjectedConfig, ParamSource
from boba.toolrun.launchers import SectionLaunchers, ToolLaunchers
from boba.toolrun.plugins import PluginKits, ToolExecution, ToolLoader
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.sandbox import StandardToolLaunchers
from boba.toolrun.stream_calls import StreamGroupsConfig
from boba.toolrun.streams import CallJournals
from boba.toolrun.tickets import SectionTickets, ServiceTickets

__all__ = ["StandRefs"]


class StandRefs:
    """Собирает запуск плагинов из переданных контекстов и ресурсов стенда."""

    STREAM_CONFIG: ClassVar[StreamGroupsConfig] = StreamGroupsConfig(
        open_sec=30.0,
        stall_sec=60.0,
        poll_sec=0.2,
        pipe_bytes=65536,
        pipe_bytes_max=1 << 30,
    )

    def __init__(
        self,
        contexts: CallContexts,
        connections: ConnectionRepository | None = None,
        credentials: CredentialSource | None = None,
    ) -> None:
        self.contexts = contexts
        self.runs = Runs(contexts)
        self.journals = CallJournals(None, self.runs)
        self.ambient = CallAmbient()
        self._connections = (
            connections if connections is not None else DisabledConnectionRepository()
        )
        self._credentials = (
            credentials
            if credentials is not None
            else KerberosCredentialSource(None, NoRefresh())
        )
        self._backends = StandardToolLaunchers().registry

        self._launchers: list[SectionLaunchers] = []

    def tool_stand(self, sources: Sequence[ParamSource]) -> ToolStand:
        return ToolStand(
            self.STREAM_CONFIG, self.contexts, self.journals, self.ambient, sources
        )

    def launchers(self, raw: DictConfig) -> SectionLaunchers:
        launchers = ToolLaunchers(raw, self._backends).build()
        self._launchers.append(launchers)

        return launchers

    def execution(
        self,
        connections: ConnectionRepository | None = None,
        credentials: CredentialSource | None = None,
    ) -> ToolExecution:
        if connections is None:
            connections = self._connections

        if credentials is None:
            credentials = self._credentials

        types = ConnectionTypes.discover()
        params = StandUserConnections(
            connections, credentials, types, self.contexts
        ).params()
        config = InjectedConfig(SectionTickets(ServiceTickets(credentials), types))

        return ToolExecution(
            self.contexts,
            self.journals,
            self.ambient,
            (params, config),
            (params,),
        )

    async def registry(
        self,
        raw: DictConfig,
        execution: ToolExecution,
        grants: ToolGrants,
        own_tools: Sequence[HostedTool] = (),
    ) -> ToolRegistry:
        launchers = self.launchers(raw)
        kits = await PluginKits(raw, launchers).load()
        return ToolLoader(raw, kits, execution, launchers, grants, own_tools).load()

    @staticmethod
    def granted(profile: str, tools: Sequence[str]) -> ToolGrants:
        wildcard = {ToolGrant.WILDCARD: ToolGrant(tools=[ToolGrant.WILDCARD])}
        profiles = {
            profile: ProfileGrant(tools=list(tools), roles=[ToolGrant.WILDCARD])
        }

        return ToolGrants(roles=wildcard, profiles=profiles, check=GrantCheck.STRICT)

    def stop(self) -> None:
        for launchers in self._launchers:
            launchers.stop()

        self._launchers.clear()
