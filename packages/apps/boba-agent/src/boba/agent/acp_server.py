"""ACP JSON-RPC: входящие данные в историю, исходящие сообщения из SQL и live-чанков."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from uuid import uuid4

from acp import RequestError
from acp.connection import Connection, StreamDirection
from acp.experimental.v2 import schema
from pydantic import ValidationError

from boba.agent import models as m
from boba.agent.history import HistoryStore, _rows
from boba.agent.queries import Query
from boba.agent.runtime import Agent, record
from boba.agent.settings import ModelSettings, SettingsLayer
from boba.agent.tools import ToolRunner, content_parts
from boba.db.postgres import AsyncPostgresPool

logger = logging.getLogger(__name__)


class SessionHub:
    """Общие исполнители всех ACP-подключений одного процесса."""

    def __init__(
        self,
        store: HistoryStore,
        llm,
        profiles: dict[str, ModelSettings],
        settings: SettingsLayer,
        version: str,
    ) -> None:
        self.store, self.llm, self.profiles = store, llm, profiles
        self.settings, self.version = settings, version
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

    async def _claim(self, sid: str) -> None:
        if self.owner_connection is not None:
            rows = await _rows(self.owner_connection, Query.OWNER_LOCK, (sid,))
            if not rows[0]["acquired"]:
                raise RuntimeError("Session is owned by another agent process")

    async def _vacancy(self) -> None:
        if len(self.agents) < self.settings.sessions.max_open:
            return
        for sid, agent in tuple(self.agents.items()):
            if agent.idle.is_set() and not agent.subscribers and not agent.background:
                await agent.close()
                if self.owner_connection is not None:
                    await _rows(self.owner_connection, Query.OWNER_UNLOCK, (sid,))
                del self.agents[sid]
                self.owners.pop(sid, None)
                return
        raise RuntimeError("Open session limit reached")

    def make(self, model, **fields):
        return record(model, self.version, **fields)

    def profile_records(self, name: str, previous=()) -> list:
        profile = self.profiles[name]
        active = {
            (item.category, item.key) for item in previous if item.operation == "set"
        }
        records: list[m.HistoryRecord] = [
            self.make(
                m.ModelRecord,
                payload={"provider": name, "value": {"model": profile.model}},
            )
        ]
        for cls, remove, value in (
            (m.SamplingRecord, m.SamplingRemovedRecord, profile.sampling),
            (m.InferenceRecord, m.InferenceRemovedRecord, profile.inference),
            (
                m.ProviderParametersRecord,
                m.ProviderParametersRemovedRecord,
                profile.provider_parameters,
            ),
        ):
            if value:
                records.append(self.make(cls, payload={"value": value}))
            elif ("setting", cls.model_fields["key"].default) in active:
                records.append(self.make(remove))
        if ("prompt", "profile") in active:
            records.append(self.make(m.SystemPromptRemovedRecord, key="profile"))
        if profile.system_prompt is not None:
            records.append(
                self.make(
                    m.SystemPromptAddedRecord,
                    key="profile",
                    payload={"content": [m.TextPart(text=profile.system_prompt)]},
                )
            )
        return records

    async def create(
        self, owner: str, granted: frozenset[str], cwd: str, *, additional=()
    ) -> Agent:
        profiles = {k: v for k, v in self.profiles.items() if k in granted}
        if not profiles:
            raise PermissionError("No permitted model profile")
        async with self.lock:
            await self._vacancy()
            sid = uuid4().hex
            await self._claim(sid)
            agent = self._agent(sid)
            values = self.profile_records(next(iter(profiles)))
            permissions = self.settings.permissions
            modes = {
                "bypassPermissions": "bypass_permissions",
                "acceptEdits": "accept_edits",
                "dontAsk": "dont_ask",
            }
            mode = modes.get(permissions.default_mode, permissions.default_mode)
            if mode == "bypass_permissions" and not permissions.allow_bypass:
                mode = "default"
            values += [
                self.make(
                    m.PermissionStateRecord,
                    payload={
                        "mode": mode,
                        "rules": [
                            *[
                                m.PermissionRule(tool=name, decision="allow")
                                for name in permissions.allow
                            ],
                            *[
                                m.PermissionRule(tool=name, decision="deny")
                                for name in permissions.deny
                            ],
                        ],
                    },
                ),
                self.make(
                    m.EnvironmentRecord,
                    key="client",
                    payload={
                        "state": {
                            "working_directory": cwd,
                            "workspace_roots": list(additional),
                            "platform": None,
                            "os_version": None,
                            "shell": None,
                            "temporary_directory": None,
                        },
                        "content": [
                            m.TextPart(text=f"Client working directory: {cwd}")
                        ],
                    },
                ),
                self.make(
                    m.ParameterOptionRecord,
                    key="model",
                    payload={
                        "name": "Model",
                        "source_kinds": ["model"],
                        "source_key": "model",
                        "value_path": [],
                        "choices": [
                            {
                                "value_id": name,
                                "name": name,
                                "value": {
                                    "provider": name,
                                    "value": {"model": profile.model},
                                },
                            }
                            for name, profile in profiles.items()
                        ],
                    },
                ),
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
            await self.store.create_session(sid, owner, cwd, values)
            self.agents[sid] = agent
            self.owners[sid] = owner
            return agent

    def _agent(self, sid: str) -> Agent:
        return Agent(
            sid,
            self.store,
            self.llm,
            self.tools,
            version=self.version,
            max_steps=self.settings.limits.max_turns,
            subscriber_buffer=self.settings.sessions.subscriber_buffer,
        )

    async def get(self, sid: str, owner: str, granted: frozenset[str]) -> Agent:
        await self.store.session(sid, owner)
        selected = await self.store.latest(sid, kind="model")
        if (
            not selected
            or m.ModelRecord.model_validate(selected[0]).payload.provider not in granted
        ):
            raise PermissionError("Session model is not permitted for this user")
        async with self.lock:
            if sid not in self.agents:
                await self._vacancy()
                await self._claim(sid)
                self.agents[sid] = self._agent(sid)
                self.owners[sid] = owner
            return self.agents[sid]

    async def launch_subagent(self, parent: Agent, reply, call):
        prompt = call.arguments["prompt"]
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("Subagent prompt must be nonempty text")
        background = call.arguments.get("background", False)
        if not isinstance(background, bool):
            raise ValueError("background must be boolean")
        owner = self.owners[parent.session_id]
        info = await self.store.session(parent.session_id, owner)
        selected = m.ModelRecord.model_validate(
            (await self.store.latest(parent.session_id, kind="model"))[0]
        )
        child = await self.create(
            owner, frozenset({selected.payload.provider}), info["cwd"]
        )
        self.tools.clients[child.session_id] = self.tools.clients.get(
            parent.session_id, {}
        ).copy()
        if parent.session_id in self.tools.ask:
            self.tools.ask[child.session_id] = self.tools.ask[parent.session_id]

        async def execute(task_id):
            inherited = await self.store.rows(
                Query.COMPACT_STATE, parent.session_id, reply.seq
            )
            values = []
            for raw in inherited:
                if raw["category"] in {"setting", "prompt"}:
                    raw.update(
                        session_id=None,
                        seq=None,
                        record_id=uuid4().hex,
                        copied_from_id=None,
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
                        "parent_request_id": reply.request_id,
                    },
                )
            )
            await child.append(values)
            await self.tools.refresh(child)
            parts = []
            agent_key = call.arguments.get("agent_key")
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


class AcpAgent:
    def __init__(
        self, hub: SessionHub, owner: str, granted: frozenset[str], version: str
    ) -> None:
        self.hub, self.owner, self.granted, self.version = hub, owner, granted, version
        self._connection: Connection | None = None
        self.initialized = False
        self.capabilities: dict = {}
        self.subscriptions = {}
        self.opening: dict = {}

    def attach(self, connection: Connection) -> None:
        self._connection = connection
        connection.add_observer(self._observe)

    @property
    def connection(self) -> Connection:
        if self._connection is None:
            raise RuntimeError("ACP connection is not attached")
        return self._connection

    def _observe(self, event):
        message = event.message
        if event.direction is StreamDirection.INCOMING:
            if (
                message.get("method") in {"session/new", "session/resume"}
                and "id" in message
            ):
                self.opening[message["id"]] = message.get("params", {}).get("sessionId")
            return None
        if (
            message.get("method") in {"session/new", "session/resume"}
            and "id" in message
        ):
            self.opening[message["id"]] = message.get("params", {}).get("sessionId")
        elif "result" in message and message.get("id") in self.opening:
            sid = self.opening.pop(message["id"])
            sid = message["result"].get("sessionId", sid)
            return self.subscribe(sid)
        elif "error" in message:
            self.opening.pop(message.get("id"), None)
        return None

    async def subscribe(self, sid: str) -> None:
        agent = await self.hub.get(sid, self.owner, self.granted)
        old = self.subscriptions.pop(sid, None)
        if old:
            agent.subscribers.discard(old)
            await old.close()
        self.subscriptions[sid] = await agent.subscribe(
            self.connection.send_notification,
            disconnect=getattr(self.connection, "close", None),
        )

    async def __call__(self, method: str, params, is_notification: bool):
        params = {} if params is None else params
        try:
            return await self.dispatch(method, params, is_notification)
        except (ValidationError, ValueError, KeyError) as exc:
            raise RequestError(-32602, "Invalid request parameters") from exc
        except (PermissionError, LookupError) as exc:
            raise RequestError(-32002, "Session or operation is not available") from exc
        except m.HistoryWriteError as exc:
            raise RequestError(
                -32003,
                "History write failed",
                {"code": exc.code, "recordIds": list(exc.record_ids)},
            ) from exc
        except RequestError:
            raise
        except Exception as exc:
            logger.exception("ACP request failed: %s", method)
            raise RequestError(
                -32603, "Agent operation failed", {"code": type(exc).__name__}
            ) from exc

    async def dispatch(self, method: str, params: dict, is_notification: bool):
        if method == "initialize":
            if is_notification or self.initialized:
                raise ValueError("Unexpected initialize")
            if params.get("protocolVersion", 0) < 2:
                raise RequestError(-32000, "ACP v2 is required")
            self.capabilities = params.get("capabilities") or {}
            self.initialized = True
            return {
                "protocolVersion": 2,
                "info": {"name": "boba", "version": self.version},
                "capabilities": {
                    "session": {
                        "prompt": {"image": {}, "embeddedContext": {}},
                        "mcp": {"acp": {}},
                    }
                },
                "authMethods": [],
            }
        if not self.initialized:
            raise RequestError(-32000, "Initialize the ACP connection first")
        if is_notification and method not in {"session/cancel", "mcp/message"}:
            return None
        if method == "session/new":
            request = schema.NewSessionRequest.model_validate(params)
            agent = await self.hub.create(
                self.owner,
                self.granted,
                request.cwd,
                additional=request.additional_directories or (),
            )
            await self._connect(agent, params)
            options = await self.hub.store.acp(
                Query.ACP_OPTIONS, agent.session_id, None
            )
            return {
                "sessionId": agent.session_id,
                "configOptions": options["update"]["configOptions"],
            }
        if method == "session/list":
            return (await self.hub.store.rows(Query.SESSION_LIST, self.owner))[0][
                "response"
            ]
        if method == "mcp/message" and is_notification:
            if params.get("method") == "notifications/tools/list_changed":
                for sid in self.subscriptions:
                    await self.hub.tools.refresh(
                        await self.hub.get(sid, self.owner, self.granted)
                    )
            return None
        if method not in {
            "session/resume",
            "session/prompt",
            "session/cancel",
            "session/close",
            "session/set_config_option",
        }:
            raise RequestError(-32601, "Method not found")
        sid = params["sessionId"]
        agent = await self.hub.get(sid, self.owner, self.granted)
        if method == "session/resume":
            schema.ResumeSessionRequest.model_validate(params)
            replay = params.get("replayFrom")
            if replay and replay.get("type") != "start":
                raise ValueError("Only replay from start is supported")
            await self._connect(agent, params)
            options = await self.hub.store.acp(Query.ACP_OPTIONS, sid, None)
            if await self.hub.store.pending_inputs(sid):
                agent.wake()
            return {"configOptions": options["update"]["configOptions"]}
        if method == "session/prompt":
            schema.PromptRequest.model_validate(params)
            content = content_parts(params["prompt"])
            if (
                len(content) == 1
                and isinstance(content[0], m.TextPart)
                and content[0].text.startswith("/")
            ):
                return {"messageId": await self.command(agent, content[0].text)}
            return {"messageId": await agent.submit(content)}
        if method == "session/cancel":
            await agent.cancel()
            return None
        if method == "session/close":
            await self.unsubscribe(sid)
            return {}
        return await self.set_option(agent, params["configId"], params["value"])

    async def _connect(self, agent: Agent, params: dict) -> None:
        if params.get("mcpServers"):
            await self.hub.tools.connect(
                agent, params["mcpServers"], self.connection.send_request
            )
        self.hub.tools.ask[agent.session_id] = self.request_action

    async def request_action(self, method: str, params: dict):
        if method == "elicitation/create":
            mode = params.get("mode", "form")
            if mode not in self.capabilities.get("elicitation", {}):
                raise ValueError("Client does not support this elicitation mode")
        return await self.connection.send_request(method, params)

    async def set_option(self, agent: Agent, key: str, selected: str) -> dict:
        options = await self.hub.store.latest(
            agent.session_id,
            key=key,
            kind=("parameter_option", "parameter_option_removed"),
        )
        if not options or options[0].operation == "remove":
            raise ValueError("Unknown parameter")
        definition = m.ParameterOptionRecord.model_validate(options[0]).payload
        choice = next(
            (item for item in definition.choices if item.value_id == selected), None
        )
        if choice is None:
            raise ValueError("Unknown choice")
        if key == "model":
            if selected not in self.granted:
                raise PermissionError("Profile is not permitted")
            previous = await self.hub.store.latest(agent.session_id)
            values = self.hub.profile_records(selected, previous)
        else:
            current = await self.hub.store.latest(
                agent.session_id,
                key=definition.source_key,
                kind=definition.source_kinds[0],
            )
            if not current:
                raise ValueError("No source for this parameter")
            value = current[0].model_dump(mode="json")
            payload = value["payload"]
            for part in definition.value_path[:-1]:
                payload = payload[part]
            if definition.value_path:
                payload[definition.value_path[-1]] = choice.value
            else:
                value["payload"] = choice.value
            value.update(record_id=uuid4().hex, session_id=None, seq=None)
            values = [m.HISTORY_RECORD_ADAPTER.validate_python(value)]
        await agent.append(values)
        result = await self.hub.store.acp(Query.ACP_OPTIONS, agent.session_id, None)
        agent.update(result["update"])
        return {"configOptions": result["update"]["configOptions"]}

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
        if subscriber:
            self.hub.agents[sid].subscribers.discard(subscriber)
            await subscriber.close()
        # Closing one observer never cancels execution shared with the other clients.
        if self.hub.tools.ask.get(sid) == self.request_action:
            self.hub.tools.ask.pop(sid)

    def busy(self) -> bool:
        return any(not self.hub.agents[sid].idle.is_set() for sid in self.subscriptions)

    async def close_all(self) -> None:
        for sid in tuple(self.subscriptions):
            await self.unsubscribe(sid)
