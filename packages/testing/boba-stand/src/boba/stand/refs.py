"""Входы приложения для стендов: только те сервисы, что стенду нужны, остальные
отказывают ServiceDisabledError при первом обращении — как процесс без секции.

Ошибки:
ServiceDisabledError — стенд попросили сервис, которого у него нет.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import ClassVar

from omegaconf import DictConfig

from boba.access import GrantCheck, ProfileGrant, ToolGrant, ToolGrants
from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.connection_broker.store import ConnectionStore
from boba.connection_broker.tools import ConnectionTools
from boba.connection_broker.user_connections import StoreRef
from boba.connections.manifest import ConnectionTypes
from boba.connections.sealed import SealKeys
from boba.identity.context import CallContexts
from boba.identity.errors import ServiceDisabledError
from boba.identity.locks import MemoryLiveLocks
from boba.identity.run import Runs
from boba.krb.seal import SsoTickets
from boba.messaging import MemoryMessageBus
from boba.messaging.bus import ListenerState, StaticBusWatch
from boba.runtime.launchers import SectionLaunchers, ToolLaunchers
from boba.runtime.plugins import EntryPointPlugins, ToolLoader
from boba.runtime.refs import RuntimeRefs
from boba.toolkit.chain import CallAmbient
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.stream_calls import StreamGroupsConfig
from boba.toolrun.streams import CallJournals

__all__ = ["StandRefs"]

TicketsRef = Callable[[], SsoTickets | None]


class StandRefs:
    """Сборка RuntimeRefs под стенд: шина, блокировки и слушатель — в памяти.

    Создаётся тестом с держателем контекста вызова своего CallStand; шина и
    блокировки — одни на объект.
    """

    HEARTBEAT_SEC: ClassVar[float] = 1.0
    LOCK_TTL_SEC: ClassVar[int] = 20
    NAME: ClassVar[str] = "stand"

    STREAM_CONFIG: ClassVar[StreamGroupsConfig] = StreamGroupsConfig(
        open_sec=30.0,
        stall_sec=60.0,
        poll_sec=0.2,
        pipe_bytes=65536,
        pipe_bytes_max=1 << 30,
    )
    """Секция [stream_groups] стендов: сроки групп и размеры пайпов каналов."""

    def __init__(self, contexts: CallContexts) -> None:
        self.contexts = contexts
        self.runs = Runs(contexts)
        self.journals = CallJournals(None, self.runs)
        self.ambient = CallAmbient()
        self._launchers: list[SectionLaunchers] = []
        self._locks = MemoryLiveLocks(self.NAME, self.LOCK_TTL_SEC)
        self._bus = MemoryMessageBus(self.NAME)

    def launchers(self, raw: DictConfig) -> SectionLaunchers:
        """Способ запуска инструментов по конфигу raw; стенд гасит его в stop()."""
        built = ToolLaunchers(raw).build()
        self._launchers.append(built)

        return built

    def registry(
        self, raw: DictConfig, refs: RuntimeRefs, grants: ToolGrants
    ) -> ToolRegistry:
        """Реестр инструментов боевым загрузчиком: плагины установленных
        пакетов, включённые в конфиге raw, и каталог соединений под правами
        grants. raw — конфиг процесса, который инструменты исполняет (сервис
        boba-mcp); права тест называет сам: у сервиса их задают endpoint'ы,
        у чата — роли и профили."""
        loader = ToolLoader(
            raw,
            EntryPointPlugins.discover(),
            refs.execution(),
            self.launchers(raw),
            grants,
            (),
            ConnectionTools(refs.connection_store, self.contexts).build(),
        )

        return loader.load()

    @staticmethod
    def granted(profile: str, tools: Sequence[str]) -> ToolGrants:
        """Права стенда: любая роль разрешает любой инструмент, профиль
        profile виден всем и отдаёт инструменты tools ('*' — все)."""
        any_role = {ToolGrant.WILDCARD: ToolGrant(tools=[ToolGrant.WILDCARD])}
        profiles = {
            profile: ProfileGrant(tools=list(tools), roles=[ToolGrant.WILDCARD])
        }

        return ToolGrants(roles=any_role, profiles=profiles, check=GrantCheck.STRICT)

    def stop(self) -> None:
        """Гасит способы запуска, построенные стендом: зиготы секций."""
        for built in self._launchers:
            built.stop()

        self._launchers.clear()

    def none(self) -> RuntimeRefs:
        """Ни реестра, ни соединений: как процесс без этих секций."""
        return self._build(self._no_registry, self._disabled_store, self._no_tickets)

    def of(self, store: StoreRef, tickets: TicketsRef) -> RuntimeRefs:
        """Соединения и билеты есть, реестра нет."""
        return self._build(self._no_registry, store, tickets)

    def _build(
        self,
        tool_registry: Callable[[], Awaitable[ToolRegistry]],
        store: StoreRef,
        tickets: TicketsRef,
    ) -> RuntimeRefs:
        def credentials() -> KerberosCredentialSource:
            return KerberosCredentialSource(tickets(), NoRefresh())

        return RuntimeRefs(
            tool_registry=tool_registry,
            connection_store=store,
            connection_types=ConnectionTypes.discover,
            credentials=credentials,
            contexts=self.contexts,
            runs=self.runs,
            journals=self.journals,
            ambient=self.ambient,
            seal_keys=SealKeys(),
            live_locks=self._live_locks,
            heartbeat_sec=self.HEARTBEAT_SEC,
            bus_watch=lambda: StaticBusWatch(ListenerState.LISTENING),
            message_bus=self._message_bus,
        )

    def _live_locks(self) -> MemoryLiveLocks:
        return self._locks

    def _message_bus(self) -> MemoryMessageBus:
        return self._bus

    @staticmethod
    async def _no_registry() -> ToolRegistry:
        msg = (
            f"resolving the tool registry: it is not part of the {StandRefs.NAME} stand"
        )
        raise ServiceDisabledError("tool", msg)

    @staticmethod
    def _disabled_store() -> ConnectionStore:
        msg = (
            "resolving the connection store: [connections] is disabled in the "
            "config, user connections are unavailable"
        )
        raise ServiceDisabledError("connections", msg)

    @staticmethod
    def _no_tickets() -> SsoTickets | None:
        return None
