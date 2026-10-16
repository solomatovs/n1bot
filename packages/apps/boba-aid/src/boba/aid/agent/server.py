"""Регистрации серверов процессов агента и запечатывание соединений в
вызовах агента: по serverId ретранслятор mcp/message узнаёт вызывающего.

serverId регистрации запуск агента (AgentLauncher) выдаёт на каждый сервер
процесса и объявляет агенту записью type "acp"; токен входа пользователя
агенту не передаётся. По serverId чат находит запись его держателя
(ProcessKeys): пользователя, тред, профиль треда и сервер; роли и секреты
входа читаются из живой сессии пользователя в момент вызова. Запечатывание
(CallSealing) находит по схеме инструмента параметры-соединения
(SealedParamSchema) и заменяет имя соединения профилем, запечатанным ключом
сервера MCP из его возможностей (SealFeature); неизвестное или невыданное
соединение — отказ с причиной.

Ошибки:
AuthenticationError — serverId неизвестен или снят либо сессия его
    держателя больше не несёт годного входа.
RefusalError — соединение неизвестно, не выдано пользователю или сервер
    инструмента не принимает соединений.
ToolConfigError — соединение не годится инструменту.
ConnectionStoreError — таблица соединений недоступна.
KerberosError — билет к соединению не выпущен.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, ClassVar
from uuid import UUID, uuid4

from acp.experimental.v2 import schema
from pydantic import ValidationError

from boba.aid.infra.session import LiveSignIn
from boba.cancellation import RunCancellation
from boba.connection_broker.user_connections import ArmedConnections
from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import (
    ConnectionSeal,
    SealedParamSchema,
    SealFeature,
    SealKey,
)
from boba.identity.context import (
    CallContext,
    CallContexts,
    Credential,
    HumanInitiator,
    Scope,
    Subject,
)
from boba.identity.errors import AuthenticationError, RefusalError
from boba.toolkit.failure import ValidationText

__all__ = [
    "CallSealing",
    "ChatCaller",
    "ChatCallers",
    "KeyHolder",
    "McpServerInfo",
    "ProcessKeys",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KeyHolder:
    """Держатель ключа процесса агента: чей это агент, какого треда и для
    какого сервера выдан ключ.

    Создаётся запуском агента (AgentLauncher): на каждый сервер, который
    процесс получает объявлением type "acp", — свой ключ, он же serverId.
    profile — профиль треда: по нему сервер инструментов чата выбирает набор
    инструментов. session — живой вход пользователя вкладки. server — имя
    сервера профиля либо сервера инструментов чата ([agent.chat_mcp].name).
    """

    user_id: UUID
    login: str
    thread_id: str
    profile: str
    session: LiveSignIn
    server: str


class ProcessKeys:
    """Ключи процессов агента этого инстанса чата: ключ → его держатель.

    Создаётся провайдером процесса, один на инстанс. Запуск агента
    регистрирует серверы процесса (register): ключ каждого — serverId его
    объявления агенту; при остановке и перезапуске ключи снимаются
    (revoke). Ретранслятор mcp/message и сервер инструментов чата находят
    по ключу держателя (holder). Реестр живёт в
    памяти: процесс агента — потомок этого же инстанса и с его рестартом не
    остаётся.
    """

    KEY_BYTES: ClassVar[int] = 32

    def __init__(self) -> None:
        self._holders: dict[str, KeyHolder] = {}

    def issue(self, holder: KeyHolder) -> str:
        """Новый случайный ключ держателя."""
        key = secrets.token_urlsafe(self.KEY_BYTES)
        self._holders[key] = holder

        return key

    def register(
        self, holder: KeyHolder, names: Sequence[str]
    ) -> list[schema.AcpMcpServer]:
        """Объявления серверов names для агента: на каждое имя новый ключ
        держателя с этим сервером, он же serverId."""
        servers: list[schema.AcpMcpServer] = []
        for name in names:
            key = self.issue(replace(holder, server=name))
            servers.append(schema.AcpMcpServer(name=name, server_id=key))

        return servers

    def revoke(self, key: str) -> None:
        """Снимает ключ; неизвестный ключ — ничего."""
        self._holders.pop(key, None)

    def holder(self, key: str) -> KeyHolder | None:
        """Держатель ключа; None — ключ не выдан этим инстансом или снят."""
        return self._holders.get(key)


@dataclass(frozen=True)
class ChatCaller:
    """Вызывающий по ключу процесса: субъект (пользователь, его роли, профиль
    треда) и секреты его входа для билетов к соединениям."""

    subject: Subject
    credential: Credential


class ChatCallers:
    """Вызывающий по Bearer ключу процесса агента.

    Создаётся провайдером процесса; им пользуются сервер инструментов (порт
    по ролям и профилю) и ретранслятор mcp/message (субъект и креды для
    соединений).
    Держателя ключа отдаёт реестр ProcessKeys; роли и секреты входа читаются
    из живой сессии держателя.
    """

    def __init__(self, keys: ProcessKeys) -> None:
        self._keys = keys

    def of_key(self, key: str) -> ChatCaller:
        """Ошибки:
        AuthenticationError — ключ неизвестен или снят либо сессия держателя
            не несёт годного входа.
        """
        holder = self._keys.holder(key)
        if holder is None:
            msg = (
                "the bearer token is not a key of a running agent process of "
                "this chat instance: expected the serverId the chat registered "
                "for the agent, got an unknown or revoked one"
            )
            raise AuthenticationError(msg)

        sign_in = holder.session.signed_in().sign_in()
        subject = Subject.of_user(
            holder.user_id, holder.login, sign_in.roles, holder.profile
        )

        return ChatCaller(subject=subject, credential=sign_in.credential())

    def context(self, caller: ChatCaller) -> CallContext:
        """Контекст вызова от агента пользователя: область пользователя,
        свой ключ запуска, инициатор — человек через API."""
        return CallContext(
            subject=caller.subject,
            scope=Scope.user(caller.subject.user_id),
            run_id=uuid4().hex,
            initiator=HumanInitiator(via="api"),
            credential=caller.credential,
            cancellation=RunCancellation(),
        )


@dataclass(frozen=True)
class McpServerInfo:
    """Сервер MCP вызываемого инструмента и возможности, которые он объявил:
    по ним запечатывание находит ключ сервера."""

    name: str
    features: Mapping[str, Mapping[str, object]]


class CallSealing:
    """Запечатывание соединений в аргументах вызова инструмента сервера MCP:
    имя соединения пользователя заменяется профилем, запечатанным ключом
    сервера, либо вызов получает отказ.

    Создаётся провайдером call_sealing; зовёт ретранслятор mcp/message.
    Какие аргументы — соединения и какого вида, читает из схемы
    инструмента (SealedParamSchema); ключ сервера — из объявленных им
    возможностей (SealFeature); соединение пользователя по виду и имени
    берёт ArmedConnections в контексте вызывающего.
    """

    def __init__(
        self,
        callers: ChatCallers,
        connections: ArmedConnections,
        contexts: CallContexts,
        ttl: timedelta,
    ) -> None:
        self._callers = callers
        self._connections = connections
        self._contexts = contexts
        self._ttl = ttl
        self._declared = SealedParamSchema()

    async def seal(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        tool_schema: Mapping[str, Any],
        server: McpServerInfo,
        caller: ChatCaller,
    ) -> dict[str, Any]:
        """Аргументы вызова tool с запечатанными соединениями на месте имён;
        у инструмента без параметров-соединений — как есть. Значение не
        строка уходит как есть, его отвергнет сервер.

        Ошибки:
        RefusalError — соединение неизвестно или не выдано пользователю либо
            сервер инструмента не принимает соединений.
        ToolConfigError — соединение не годится инструменту.
        ConnectionStoreError — таблица соединений недоступна.
        KerberosError — билет к соединению не выпущен.
        """
        updated: dict[str, Any] = dict(arguments)
        kinds = self._declared.kinds_of(tool_schema)
        if not kinds:
            return updated

        seal = ConnectionSeal(self._key(server))
        with self._contexts.applied(self._callers.context(caller)):
            for param, kind in kinds.items():
                name = updated.get(param)
                if not isinstance(name, str):
                    continue

                sealed = await self._connections.sealed(kind, name, self._ttl)
                updated[param] = seal.seal(sealed)

                logger.info(
                    "sealing: connection %s of kind %s sealed in %s of %s for %s",
                    name,
                    kind,
                    param,
                    tool,
                    caller.subject.login,
                )

        return updated

    def _key(self, server: McpServerInfo) -> SealKey:
        """Ключ сервера инструмента из возможностей, которые он объявил.

        Ошибки:
        RefusalError — сервер не принимает соединений.
        """
        declared = server.features.get(SealFeature.ID)
        if declared is None:
            msg = (
                f"mcp server {server.name!r} does not declare the feature "
                f"{SealFeature.ID!r}, so it does not accept connections"
            )
            raise RefusalError(ConnectionRefusal.SEAL_KEY_UNKNOWN, msg)

        try:
            return SealFeature.model_validate(declared).key
        except ValidationError as exc:
            msg = (
                f"mcp server {server.name!r}: the feature {SealFeature.ID!r} does "
                f"not carry an encryption key: {ValidationText.of(exc)}"
            )
            raise RefusalError(ConnectionRefusal.SEAL_KEY_UNKNOWN, msg) from exc
