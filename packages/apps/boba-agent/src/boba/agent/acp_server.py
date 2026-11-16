"""ACP JSON-RPC: входящие данные в историю, исходящие сообщения из SQL и live-чанков."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import (
    AsyncGenerator,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
    Set,
)
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from uuid import uuid4

import mcp.types as mt
from acp import RequestError
from acp.connection import Connection
from pydantic import ValidationError

from boba.agent import models as m
from boba.agent.history import HistoryStore, _rows
from boba.agent.llm import Providers
from boba.agent.queries import Query
from boba.agent.records import RecordFactory
from boba.agent.runtime import Agent, SessionProtocol, Subscription
from boba.agent.settings import (
    AgentProfile,
    ModelSettings,
    NamedProfile,
    SettingsError,
    SettingsLayer,
)
from boba.agent.tools import McpNegotiationError, McpPeer, ToolRunner
from boba.db.postgres import AsyncPostgresPool
from boba.identity.signin import ProfileCatalog

logger = logging.getLogger(__name__)


class AgentProfiles(ProfileCatalog):
    """Каталог настроек моделей для входа и SessionHub, без HTTP-подключений.

    Сборка службы создаёт каталог из конфига; каталог выбирает профили
    по выданным именам и предоставляет варианты выбора для истории.
    """

    def __init__(self, profiles: Mapping[str, AgentProfile]) -> None:
        if not profiles:
            raise SettingsError("Agent profile catalog requires at least one profile")

        named = self._named(profiles)
        self._profiles = dict(named)

    def _named(
        self, profiles: Mapping[str, AgentProfile]
    ) -> Iterator[tuple[str, NamedProfile]]:
        for name, config in profiles.items():
            if not name:
                raise SettingsError("Agent profile name must not be empty")

            yield name, NamedProfile(name, config)

    def __iter__(self) -> Iterator[NamedProfile]:
        profiles = self._profiles.values()

        return iter(profiles)

    def available(self, granted: Set[str]) -> Iterator[NamedProfile]:
        for profile in self._profiles.values():
            if profile.name not in granted:
                continue

            yield profile

    def default(self, granted: Set[str]) -> NamedProfile:
        """Первый доступный профиль в порядке конфигурации."""
        available = self.available(granted)
        profile = next(available, None)
        if profile is None:
            raise PermissionError("No permitted model profile")

        return profile

    def require(self, name: str, granted: Set[str]) -> NamedProfile:
        """Проверяет существование профиля и право вызывающего выбрать профиль."""
        if name not in granted:
            raise PermissionError(f"Agent profile {name!r} is not permitted")

        profile = self._profiles.get(name)
        if profile is None:
            raise LookupError(f"Agent profile {name!r} is not configured")

        return profile

    def choices(self, granted: Set[str]) -> tuple[m.ParameterChoice, ...]:
        """Варианты model для ParameterOptionRecord; только выданные профили."""
        choices = self._choices(granted)

        return tuple(choices)

    def _choices(self, granted: Set[str]) -> Iterator[m.ParameterChoice]:
        for profile in self.available(granted):
            yield m.ParameterChoice(
                value_id=profile.name,
                name=profile.name,
                value=profile.name,
            )

    def granted_by_roles(self, user_roles: frozenset[str]) -> frozenset[str]:
        names = self._visible_names(user_roles)

        return frozenset(names)

    def _visible_names(self, user_roles: frozenset[str]) -> Iterator[str]:
        for profile in self._profiles.values():
            if not profile.config.visible_for(user_roles):
                continue

            yield profile.name

    def known(self, name: str) -> bool:
        return name in self._profiles


class SessionHub:
    """Общие исполнители всех ACP-подключений одного процесса."""

    def __init__(
        self,
        store: HistoryStore,
        llm,
        profiles: AgentProfiles,
        settings: SettingsLayer,
        version: str,
        *,
        providers: Providers,
    ) -> None:
        self.store = store
        self.llm = llm
        self.profiles = profiles
        self.settings = settings
        self.version = version
        self.records = RecordFactory(version)
        self.providers = providers
        self.tools = ToolRunner(
            list_timeout=settings.mcp.list_timeout_sec,
            call_timeout=settings.mcp.call_timeout_sec,
            input_rounds=settings.mcp.input_rounds,
        )
        self.tools.launch_agent = self.launch_subagent
        self.agents: dict[str, Agent] = {}
        self.owners: dict[str, str] = {}
        self.lock = asyncio.Lock()
        self.owner_connection = None
        self.owner_watch = None

    async def start(self, postgres) -> None:
        self.owner_connection = await AsyncPostgresPool.dedicated(postgres)
        self.owner_watch = asyncio.create_task(self._watch_owner())

    async def _watch_owner(self) -> None:
        connection = self.owner_connection
        if connection is None:
            raise RuntimeError("Ownership connection is not open")
        try:
            while True:
                await connection.execute("select 1")
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Execution ownership connection lost")
            for agent in tuple(self.agents.values()):
                await agent.close()

    @asynccontextmanager
    async def _claim(self, sid: str) -> AsyncGenerator[None, None]:
        """Удерживает владение сессией; при неудачном открытии снимает блокировку."""
        if self.owner_connection is not None:
            rows = await _rows(self.owner_connection, Query.OWNER_LOCK, (sid,))
            if not rows[0]["acquired"]:
                raise RuntimeError(f"Session {sid} is owned by another agent process")

        try:
            yield

        finally:
            registered = sid in self.agents
            connection = self.owner_connection
            if not registered and connection is not None:
                await _rows(connection, Query.OWNER_UNLOCK, (sid,))

    async def _vacancy(self, *, protected: str | None = None) -> None:
        if len(self.agents) < self.settings.sessions.max_open:
            return
        for sid, agent in tuple(self.agents.items()):
            if sid == protected:
                continue

            if agent.idle.is_set() and not agent.subscribers and not agent.background:
                await agent.close()
                if self.owner_connection is not None:
                    await _rows(self.owner_connection, Query.OWNER_UNLOCK, (sid,))
                del self.agents[sid]
                self.owners.pop(sid, None)
                return
        raise RuntimeError("Open session limit reached")

    def make(self, model, **fields):
        return self.records.make(model, **fields)

    def _active_record_keys(
        self, records: Iterable[m.HistoryRecord]
    ) -> Iterator[tuple[str, str | None]]:
        for item in records:
            if item.operation != "set":
                continue

            yield item.category, item.key

    def _profile_settings(
        self, profile: ModelSettings, active: Set[tuple[str, str | None]]
    ) -> Iterator[m.HistoryRecord]:
        if profile.sampling:
            yield self.make(m.SamplingRecord, payload={"value": profile.sampling})
        elif ("setting", "sampling") in active:
            yield self.make(m.SamplingRemovedRecord)

        if profile.inference:
            yield self.make(m.InferenceRecord, payload={"value": profile.inference})
        elif ("setting", "inference") in active:
            yield self.make(m.InferenceRemovedRecord)

        if profile.provider_parameters:
            yield self.make(
                m.ProviderParametersRecord,
                payload={"value": profile.provider_parameters},
            )
        elif ("setting", "provider_parameters") in active:
            yield self.make(m.ProviderParametersRemovedRecord)

    def profile_to_records(
        self,
        profile: NamedProfile,
        *,
        provider: str | None,
        previous: Iterable[m.HistoryRecord] = (),
    ) -> list[m.HistoryRecord]:
        record_keys = self._active_record_keys(previous)
        active = set(record_keys)

        records: list[m.HistoryRecord] = [
            self.make(
                m.ModelRecord,
                payload={
                    "provider": provider,
                    "profile_id": profile.name,
                    "value": {"model": profile.settings.model},
                },
            )
        ]

        settings = self._profile_settings(profile.settings, active)
        records.extend(settings)

        if ("prompt", "profile") in active:
            records.append(self.make(m.SystemPromptRemovedRecord, key="profile"))

        if profile.settings.system_prompt is not None:
            records.append(
                self.make(
                    m.SystemPromptAddedRecord,
                    key="profile",
                    payload={
                        "content": [m.TextPart(text=profile.settings.system_prompt)]
                    },
                )
            )
        return records

    def _model_option(
        self, choices: Sequence[m.ParameterChoice]
    ) -> m.ParameterOptionRecord:
        return self.make(
            m.ParameterOptionRecord,
            key="model",
            payload={
                "name": "Model",
                "source_kinds": ["model"],
                "source_key": "model",
                "value_path": ["profile_id"],
                "choices": choices,
            },
        )

    def _provider_option(
        self, provider_ids: Sequence[str]
    ) -> m.ParameterOptionRecord | None:
        if not provider_ids:
            return None

        choices = []
        for provider_id in provider_ids:
            choices.append(
                m.ParameterChoice(
                    value_id=provider_id, name=provider_id, value=provider_id
                )
            )

        return self.make(
            m.ParameterOptionRecord,
            key="provider",
            payload={
                "name": "Provider",
                "source_kinds": ["model"],
                "source_key": "model",
                "value_path": ["provider"],
                "choices": choices,
            },
        )

    async def _update_option(
        self, agent: Agent, option: m.ParameterOptionRecord
    ) -> bool:
        current = await self.store.latest(
            agent.session_id, kind="parameter_option", key=option.key
        )
        if current:
            previous = m.ParameterOptionRecord.model_validate(current[0])
            if previous.payload == option.payload:
                return False

        await agent.append([option])

        return True

    async def refresh_provider_options(self, agent: Agent, owner: str) -> None:
        provider_ids = await self.providers.choices(owner)
        option = self._provider_option(provider_ids)
        if option is None:
            return

        selected = await self.store.latest(agent.session_id, kind="model")
        model = m.ModelRecord.model_validate(selected[0])
        if model.payload.provider is None:
            payload = model.payload.model_copy(update={"provider": provider_ids[0]})
            await agent.append([self.make(m.ModelRecord, payload=payload)])

        if await self._update_option(agent, option):
            await agent.options_changed()

    async def _restore_model_profile(
        self, agent: Agent, granted: frozenset[str]
    ) -> None:
        selected = await self.store.latest(agent.session_id, kind="model")
        model = m.ModelRecord.model_validate(selected[0])
        if model.payload.profile_id is None:
            options = await self.store.latest(
                agent.session_id, kind="parameter_option", key="model"
            )
            if not options:
                return

            definition = m.ParameterOptionRecord.model_validate(options[0]).payload
            value = model.payload.model_dump(mode="json", exclude={"profile_id"})
            for part in definition.value_path:
                value = value[part]

            matches = []
            for choice in definition.choices:
                if choice.value == value:
                    matches.append(choice.value_id)

            if len(matches) != 1:
                raise SettingsError(
                    f"Cannot restore model profile for session {agent.session_id!r}: "
                    f"expected one saved selection, got {len(matches)}"
                )

            self.profiles.require(matches[0], granted)
            payload = model.payload.model_copy(update={"profile_id": matches[0]})
            await agent.append([self.make(m.ModelRecord, payload=payload)])

        if choices := self.profiles.choices(granted):
            await self._update_option(agent, self._model_option(choices))

    def _permission_rules(self) -> Iterator[m.PermissionRule]:
        for name in self.settings.permissions.allow:
            yield m.PermissionRule(tool=name, decision="allow")

        for name in self.settings.permissions.deny:
            yield m.PermissionRule(tool=name, decision="deny")

    def _environment(self, cwd: str, additional: Sequence[str]) -> m.EnvironmentRecord:
        return self.make(
            m.EnvironmentRecord,
            key="client",
            payload={
                "state": {
                    "working_directory": cwd,
                    "workspace_roots": tuple(additional),
                    "platform": None,
                    "os_version": None,
                    "shell": None,
                    "temporary_directory": None,
                },
                "content": [m.TextPart(text=f"Client working directory: {cwd}")],
            },
        )

    async def create(
        self,
        owner: str,
        granted: frozenset[str],
        cwd: str,
        *,
        additional: Sequence[str] = (),
        inherited_model: m.ModelRecord | None = None,
    ) -> Agent:
        choices = self.profiles.choices(granted)
        provider_ids = await self.providers.choices(owner)
        values: list[m.HistoryRecord]

        if inherited_model is None:
            profile = self.profiles.default(granted)
            provider = next(iter(provider_ids), None)
            values = self.profile_to_records(profile, provider=provider)
        else:
            values = [self.make(m.ModelRecord, payload=inherited_model.payload)]

        if provider_option := self._provider_option(provider_ids):
            values.append(provider_option)

        if choices:
            values.append(self._model_option(choices))

        async with self.lock:
            await self._vacancy()
            sid = uuid4().hex
            agent = self._agent(sid, owner)

            permissions = self.settings.permissions
            values += [
                self.make(
                    m.PermissionStateRecord,
                    payload={
                        "mode": permissions.mode,
                        "rules": tuple(self._permission_rules()),
                    },
                ),
                self.make(
                    m.ParameterOptionRecord,
                    key="mode",
                    payload={
                        "name": "Tool permissions",
                        "source_kinds": ["permission_state"],
                        "source_key": None,
                        "value_path": ["mode"],
                        "choices": tuple(permissions.choices()),
                    },
                ),
                self._environment(cwd, additional),
                self.make(
                    m.CommandAddedRecord,
                    key="compact",
                    payload={
                        "description": "Summarize the conversation",
                        "owner": {"kind": "agent"},
                    },
                ),
                *self.tools.builtins(agent),
            ]
            async with self._claim(sid):
                await self.store.create_session(
                    sid, owner, cwd, self.records.group(values, uuid4().hex)
                )
                self.agents[sid] = agent
                self.owners[sid] = owner

            return agent

    def _agent(self, sid: str, owner: str) -> Agent:
        inference = self.llm.for_user(owner, self.providers)

        return Agent(
            sid,
            self.store,
            inference,
            self.tools,
            version=self.version,
            max_steps=self.settings.limits.max_turns,
            subscriber_buffer=self.settings.sessions.subscriber_buffer,
        )

    async def get(self, sid: str, owner: str, granted: frozenset[str]) -> Agent:
        await self.store.session(sid, owner)
        selected = await self.store.latest(sid, kind="model")
        model = m.ModelRecord.model_validate(selected[0])
        if model.payload.profile_id is not None:
            self.profiles.require(model.payload.profile_id, granted)

        async with self.lock:
            await self.store.session(sid, owner)
            if agent := self.agents.get(sid):
                return agent

            await self._vacancy()
            async with self._claim(sid):
                self.agents[sid] = self._agent(sid, owner)
                self.owners[sid] = owner

            agent = self.agents[sid]

        await self._restore_model_profile(agent, granted)
        await self.refresh_provider_options(agent, owner)

        return agent

    async def delete(self, sid: str, owner: str) -> None:
        """Останавливает общий исполнитель и сохраняет запрет дальнейшего открытия."""
        async with self.lock:
            visible = await self.store.rows(Query.SESSION_GET, sid, owner)
            if not visible:
                return

            agent = self.agents.get(sid)
            if agent is None:
                async with self._claim(sid):
                    await self.store.append(sid, [self.make(m.SessionDeletedRecord)])

                return

            await agent.close()
            await self.store.append(sid, [self.make(m.SessionDeletedRecord)])
            self.agents.pop(sid)
            self.owners.pop(sid, None)
            try:
                await self.tools.release_session(sid)

            finally:
                if self.owner_connection is not None:
                    await _rows(self.owner_connection, Query.OWNER_UNLOCK, (sid,))

    async def fork(
        self,
        sid: str,
        owner: str,
        granted: frozenset[str],
        cwd: str,
        additional: Sequence[str],
    ) -> Agent:
        """Создаёт независимую сессию из завершённой истории без запуска сохранённых действий."""
        source = await self.get(sid, owner, granted)
        async with self.lock, source.lock:
            if not source.idle.is_set():
                raise ValueError(f"Cannot fork running session {sid!r}")

            if source.background:
                raise ValueError(f"Cannot fork session {sid!r} with background work")

            await self._vacancy(protected=sid)
            through_seq = await self.store.head(sid)
            target_id = uuid4().hex
            marker = self.make(
                m.SessionForkedRecord,
                payload={"source_session_id": sid, "through_seq": through_seq},
            )
            async with self._claim(target_id):
                target = self._agent(target_id, owner)
                environment = self._environment(cwd, additional)
                await self.store.fork_session(
                    sid, target_id, owner, cwd, marker, [environment]
                )
                self.agents[target_id] = target
                self.owners[target_id] = owner

            return target

    async def launch_subagent(self, parent: Agent, reply, call):
        prompt = call.payload.arguments["prompt"]
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("Subagent prompt must be nonempty text")
        background = call.payload.arguments.get("background", False)
        if not isinstance(background, bool):
            raise ValueError("background must be boolean")
        owner = self.owners[parent.session_id]
        info = await self.store.session(parent.session_id, owner)
        selected = m.ModelRecord.model_validate(
            (await self.store.latest(parent.session_id, kind="model"))[0]
        )
        child = await self.create(
            owner, frozenset(), info["cwd"], inherited_model=selected
        )
        self.tools.clients[child.session_id] = self.tools.clients.get(
            parent.session_id, {}
        ).copy()
        if parent.session_id in self.tools.ask:
            self.tools.ask[child.session_id] = self.tools.ask[parent.session_id]

        async def execute(task_id):
            inherited = await self.store.rows(
                Query.COMPACT_STATE, parent.session_id, reply.seq, None
            )
            values = []
            for item in inherited:
                raw = item["record"]
                if raw["category"] in {"setting", "prompt"}:
                    raw.update(
                        session_id=None,
                        seq=None,
                        record_id=uuid4().hex,
                        copied_from_id=None,
                        operation_id=uuid4().hex,
                        operation_position=0,
                        parent_operation_id=None,
                    )
                    if raw["category"] == "prompt":
                        existing = await self.store.latest(
                            child.session_id, category="prompt", key=raw["key"]
                        )
                        if existing and existing[0].operation == "set":
                            values.append(
                                child.make(m.SystemPromptRemovedRecord, key=raw["key"])
                            )
                    values.append(m.HISTORY_RECORD_ADAPTER.validate_python(raw))
            permissions = await self.store.latest(
                parent.session_id, kind="permission_state"
            )
            if permissions:
                values.append(
                    child.make(m.PermissionStateRecord, payload=permissions[0].payload)
                )
            values.append(
                child.make(
                    m.SubagentSessionRecord,
                    payload={
                        "parent_session_id": parent.session_id,
                        "parent_task_id": task_id,
                        "context_through_seq": reply.seq,
                    },
                )
            )
            await child.append(child.records.group(values, uuid4().hex))
            await self.tools.refresh(child)
            parts = []
            agent_key = call.payload.arguments.get("agent_key")
            if agent_key is not None:
                agent_type = await self.store.latest(
                    parent.session_id, category="context", key=agent_key
                )
                if not agent_type or not isinstance(agent_type[0], m.AgentAddedRecord):
                    raise ValueError("Unknown subagent definition")
                parts.extend(agent_type[0].payload.content)
            parts.append(m.TextPart(text=prompt))
            await child.submit(parts)
            try:
                await child.idle.wait()
                if child.last_error is not None:
                    raise RuntimeError(
                        "Subagent execution failed"
                    ) from child.last_error
                last = await self.store.latest(
                    child.session_id, kind="assistant_message"
                )
                return tuple(
                    part
                    for part in m.AssistantMessageRecord.model_validate(
                        last[0]
                    ).payload.content
                    if isinstance(part, m.TextPart)
                )
            except asyncio.CancelledError:
                await child.cancel()
                raise

        return await parent.task(
            reply,
            call,
            execute,
            background=background,
            child_session_id=child.session_id,
        )

    async def close(self) -> None:
        for agent in tuple(self.agents.values()):
            await agent.close()
        self.agents.clear()
        if self.owner_watch is not None:
            self.owner_watch.cancel()
            with suppress(asyncio.CancelledError):
                await self.owner_watch
        if self.owner_connection is not None:
            await self.owner_connection.close()


class AcpOperations:
    """Общие операции подключения: права, история, tools, команды и подписки.

    AcpAgent создаёт объект; выбранная реализация протокола вызывает операции,
    не преобразуя форматы другой версии ACP.
    """

    def __init__(
        self, hub: SessionHub, owner: str, granted: frozenset[str], version: str
    ) -> None:
        self.hub = hub
        self.owner = owner
        self.granted = granted
        self.version = version
        self._connection: Connection | None = None
        self.capabilities: dict = {}
        self.subscriptions: dict[str, Subscription] = {}
        self.actions: dict[str, Callable] = {}
        self.mcp_peers: set[McpPeer] = set()

    @asynccontextmanager
    async def errors(self) -> AsyncGenerator[None, None]:
        try:
            yield
        except (ValidationError, ValueError, KeyError) as exc:
            raise RequestError(-32602, str(exc)) from exc
        except (PermissionError, LookupError) as exc:
            raise RequestError(-32002, str(exc)) from exc
        except m.HistoryWriteError as exc:
            raise RequestError(
                -32003,
                "History write failed",
                {"code": exc.code, "recordIds": list(exc.record_ids)},
            ) from exc
        except McpNegotiationError as exc:
            raise RequestError(-32004, str(exc)) from exc
        except RequestError:
            raise
        except Exception as exc:
            raise RequestError(-32603, f"Agent operation failed: {exc}") from exc

    def attach(self, connection: Connection) -> None:
        self._connection = connection

    @property
    def connection(self) -> Connection:
        if self._connection is None:
            raise RuntimeError("ACP connection is not attached")
        return self._connection

    async def subscribe(
        self, protocol: SessionProtocol, sid: str, *, replay_history: bool = False
    ) -> None:
        agent = await self.hub.get(sid, self.owner, self.granted)
        old = self.subscriptions.pop(sid, None)
        if old is not None:
            agent.subscribers.discard(old)
            await old.close()

        self.subscriptions[sid] = await agent.subscribe(
            self.connection.send_notification,
            protocol,
            replay_history=replay_history,
            disconnect=getattr(self.connection, "close", None),
        )

    async def restore(self, sid: str, cwd: str) -> Agent:
        session = await self.hub.store.session(sid, self.owner)
        if cwd != session["cwd"]:
            raise ValueError(f"Session {sid} requires cwd {session['cwd']}")

        agent = await self.hub.get(sid, self.owner, self.granted)

        return agent

    async def open(
        self,
        agent: Agent,
        params: dict,
        protocol: SessionProtocol,
        *,
        replay_history: bool = False,
    ) -> None:
        await self._connect(agent, params, protocol)
        await self.subscribe(protocol, agent.session_id, replay_history=replay_history)
        if await self.hub.store.pending_messages(agent.session_id):
            agent.wake()

    @asynccontextmanager
    async def created_session(
        self, agent: Agent, params: dict, protocol: SessionProtocol
    ) -> AsyncGenerator[None, None]:
        """Неудачное открытие нового чата освобождает ресурсы и закрывает созданную сессию."""
        async with AsyncExitStack() as opening:
            opening.push_async_callback(self.hub.delete, agent.session_id, self.owner)
            if not params.get("mcpServers"):
                await self.hub.tools.refresh(agent)

            await self.open(agent, params, protocol)
            yield
            opening.pop_all()

    async def _connect(
        self, agent: Agent, params: dict, protocol: SessionProtocol
    ) -> None:
        if params.get("mcpServers"):
            capabilities = mt.ClientCapabilities(roots=mt.RootsCapability())
            elicitation = self.capabilities.get("elicitation")
            if elicitation is not None:
                capabilities.elicitation = mt.ElicitationCapability.model_validate(
                    elicitation
                )

            peers = await self.hub.tools.connect(
                agent,
                params["mcpServers"],
                self.connection.send_request,
                self.connection.send_notification,
                self.mcp_peers,
                capabilities,
            )
            self.mcp_peers.update(peers)
        self.hub.tools.ask[agent.session_id] = protocol.request_action
        self.actions[agent.session_id] = protocol.request_action

    async def mcp_message(
        self, connection_id: str, method: str, params: dict | None
    ) -> dict:
        async with self.errors():
            for peer in self.mcp_peers:
                if peer.connection_id != connection_id:
                    continue

                return await self.hub.tools.incoming(peer, method, params)

            raise RequestError(-32602, f"Unknown MCP connection {connection_id!r}")

    async def list_providers(self, query: Query) -> dict:
        rows = await self.hub.store.rows(query, self.owner)

        return rows[0]["response"]

    async def set_provider(
        self,
        provider_id: str,
        api_type: str,
        base_url: str,
        headers: Mapping[str, str] | None,
    ) -> None:
        registry = self.hub.providers
        if api_type != "openai":
            raise ValueError(
                f"Provider {provider_id!r} supports API 'openai', got {api_type!r}"
            )

        request_headers = headers
        if request_headers is None:
            request_headers = {}

        await registry.set(
            self.owner,
            provider_id,
            base_url,
            request_headers,
        )

        for session_id, agent in tuple(self.hub.agents.items()):
            if self.hub.owners[session_id] != self.owner:
                continue

            await self.hub.refresh_provider_options(agent, self.owner)

    async def disable_provider(self, provider_id: str) -> None:
        registry = self.hub.providers
        await registry.disable(self.owner, provider_id)

    async def set_option(self, agent: Agent, key: str, selected: str) -> None:
        expected_seq = await self.hub.store.head(agent.session_id)
        options = await self.hub.store.latest(
            agent.session_id,
            key=key,
            kind=("parameter_option", "parameter_option_removed"),
        )
        if not options:
            raise ValueError("Unknown parameter")

        if options[0].operation == "remove":
            raise ValueError("Unknown parameter")

        definition = m.ParameterOptionRecord.model_validate(options[0]).payload
        choice = definition.choose(selected)
        if key == "model":
            profile = self.hub.profiles.require(selected, self.granted)
            previous = await self.hub.store.latest(agent.session_id)
            selected_model = await self.hub.store.latest(agent.session_id, kind="model")
            model = m.ModelRecord.model_validate(selected_model[0])
            values = self.hub.profile_to_records(
                profile, provider=model.payload.provider, previous=previous
            )
        else:
            current = await self.hub.store.rows(
                Query.PARAMETER_SOURCE,
                agent.session_id,
                definition.source_kinds[0],
                definition.source_key,
            )
            if not current:
                raise ValueError("No source for this parameter")
            source = m.HISTORY_RECORD_ADAPTER.validate_python(current[0]["record"])
            updated = source.payload.model_dump(mode="python")
            payload = updated
            for part in definition.value_path[:-1]:
                payload = payload[part]
            if definition.value_path:
                payload[definition.value_path[-1]] = choice.value
            else:
                updated = choice.value

            values = [self.hub.make(type(source), key=source.key, payload=updated)]

        await agent.append(
            agent.records.group(values, uuid4().hex), expected_seq=expected_seq
        )
        await agent.options_changed()

    async def command(self, agent: Agent, text: str) -> str:
        name, _, rest = text[1:].partition(" ")
        available = await self.hub.store.latest(
            agent.session_id, kind=("command_added", "command_removed"), key=name
        )
        if not available or available[0].operation == "remove":
            raise ValueError("Command not available")
        owner = m.CommandAddedRecord.model_validate(available[0]).payload.owner
        call = agent.make(
            m.SlashCommandRecord,
            payload={
                "name": name,
                "text": text,
                "arguments": {"text": rest},
                "owner": owner,
            },
        )
        await agent.append([call])
        outcome, error = "succeeded", None
        try:
            if owner.kind == "agent" and name == "compact":
                await self.hub.tools.compact(agent)
            else:
                raise ValueError("No executor for this command owner")
        except Exception as exc:
            outcome, error = "failed", type(exc).__name__
            raise
        finally:
            await agent.append(
                [
                    agent.make(
                        m.SlashCommandResultRecord,
                        parent_operation_id=call.operation_id,
                        payload={
                            "command_id": call.record_id,
                            "outcome": outcome,
                            "error": error,
                            "structured_content": None,
                        },
                    )
                ]
            )
        return call.record_id

    async def unsubscribe(self, sid: str) -> None:
        subscriber = self.subscriptions.pop(sid, None)
        if subscriber is not None:
            agent = self.hub.agents.get(sid)
            if agent is not None:
                agent.subscribers.discard(subscriber)

            await subscriber.close()
        # Closing one observer never cancels execution shared with the other clients.
        action = self.actions.pop(sid, None)
        if self.hub.tools.ask.get(sid) == action:
            self.hub.tools.ask.pop(sid, None)

    def busy(self) -> bool:
        for sid in self.subscriptions:
            agent = self.hub.agents.get(sid)
            if agent is None:
                continue

            if not agent.idle.is_set():
                return True

        return False

    async def close_all(self) -> None:
        for sid in tuple(self.subscriptions):
            await self.unsubscribe(sid)

        peers = tuple(self.mcp_peers)
        self.mcp_peers.clear()
        async with AsyncExitStack() as closing:
            for peer in peers:
                closing.push_async_callback(self.hub.tools.close_peer, peer)
