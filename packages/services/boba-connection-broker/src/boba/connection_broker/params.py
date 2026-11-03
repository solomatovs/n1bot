"""Разрешение имён connections перед исполнением инструментов MCP.

Ошибки:
RefusalError — имя недоступно субъекту, неоднозначно или нет credentials.
ToolConfigError — некорректное объявление параметра или профиль.
KerberosError — не удалось подготовить билет вызова.
ConnectionStoreError — каталог MCP недоступен."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, ClassVar
from uuid import UUID

from pydantic import AfterValidator, ConfigDict, Field
from pydantic.fields import FieldInfo

from boba.access import RoleVisibility
from boba.connection_broker.user_connections import (
    ArmedConnections,
    ConnectionParamHooks,
    TypesRef,
)
from boba.connections.base import ClientIdentity, ConnectionBase
from boba.connections.manifest import ConnectionTypesError
from boba.connections.marks import ConnectionRefusal
from boba.connections.offers import (
    ConnectionOffer,
    OfferedSource,
)
from boba.connections.whitelist import ConnectionWhitelist
from boba.identity.context import CallContexts
from boba.identity.errors import RefusalError
from boba.toolrun.hosted import HostedTool, ToolSchema
from boba.toolrun.injected import (
    AsyncInjected,
    ConfigArming,
    ParamSource,
    ToolConfigError,
)
from boba.toolrun.stream_calls import NodeArgs

__all__ = ["ConnectionParam", "ConnectionParams", "ServerConnection"]

logger = logging.getLogger(__name__)


class ServerConnection(RoleVisibility):
    """Заданное соединение сервера, секция [connections.<имя>]: вид, описание
    для модели, роли, которым оно видно, и профиль под сервисной учёткой
    (keytab либо пароль из секретов site).

    Конфиг сервиса boba-mcp разбирает секцию этой моделью на старте;
    ConnectionParams один раз собирает из неё профиль вида kind и
    дальше держит только его. Делегирования у таких соединений нет.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    description: str = Field(min_length=1)
    connection: dict[str, Any]


@dataclass(frozen=True)
class ConnectionValue:
    """Проверенный профиль и источник, определяющий подготовку credentials."""

    profile: ConnectionBase
    source: OfferedSource


class ConnectionParam(AsyncInjected):
    """Подставляет профиль вместо имени параметра, затем готовит credentials.

    Создаётся ConnectionParams; grants проверяет ArmedConnections в контексте вызова."""

    APPLICATION: ClassVar[str] = "boba"
    """Имя приложения в подписи профиля: под ним ходят все инструменты."""

    def __init__(  # noqa: PLR0913 — обвязка собирается всеми входами параметра
        self,
        contexts: CallContexts,
        tickets: ConfigArming,
        listed: ConnectionWhitelist,
        param: str,
        kind: str,
        visibility: Mapping[str, ServerConnection],
        stored: ArmedConnections | None = None,
    ) -> None:
        super().__init__(param, None)
        self._contexts = contexts
        self._tickets = tickets
        self._listed = listed
        self.kind = kind
        self._stored = stored
        self._visibility = visibility

    async def value(self, name: str, kwargs: dict[str, object]) -> object:
        value = kwargs.get(self._param)
        resolved = await self.verified(name, value)
        connection = resolved.profile
        if resolved.source is OfferedSource.STORED:
            resolver = self._stored
            if resolver is None:
                msg = f"{name}: the stored connection has no credential resolver"
                raise ToolConfigError(msg)

            connection = await resolver.armed(connection)

        login = self._contexts.subject().login
        client = ClientIdentity(application=self.APPLICATION, login=login, tool=name)
        labelled = connection.labeled(client)

        logger.info(
            "tool %s: connection in %s (%s) %s",
            name,
            self._param,
            self.kind,
            labelled.trace(),
        )

        if not self._tickets.needs(labelled):
            return labelled

        return await self._tickets.armed(self._param, labelled)

    async def verified(self, name: str, value: object) -> ConnectionValue:
        """Проверяет имя и текущие права до выдачи credentials и запуска тела."""
        if not isinstance(value, str):
            msg = (
                f"{name}: {self._param!r} expects a connection name, "
                f"got {type(value).__name__}"
            )
            raise RefusalError(ConnectionRefusal.NOT_VISIBLE, msg)

        server = self._listed.pick(value)
        if server is not None:
            roles = self._contexts.subject().roles
            if not self._visibility[value].visible_for(roles):
                msg = f"{name}: the configured connection is not granted to your roles"
                raise RefusalError(ConnectionRefusal.NOT_VISIBLE, msg)

            return ConnectionValue(server, OfferedSource.SERVER)

        if self._stored is None:
            msg = f"{name}: the connection name is not available to this caller"
            raise RefusalError(ConnectionRefusal.NOT_VISIBLE, msg)

        connection = await self._stored.available(self.kind, value)
        return ConnectionValue(connection, OfferedSource.STORED)


class ConnectionParams(NodeArgs, ParamSource):
    """Ставит серверные resolver-обвязки и публикует схемы имён connections.

    Собирается runtime. Реализует ParamSource и NodeArgs: проверяет права
    на профили при вызове инструмента и до запуска DAG."""

    def __init__(
        self,
        types_ref: TypesRef,
        contexts: CallContexts,
        servers: Mapping[str, ServerConnection],
        tickets: ConfigArming,
        stored: ArmedConnections | None = None,
    ) -> None:
        self._types_ref = types_ref
        self._contexts = contexts
        self._tickets = tickets
        self._stored = stored
        self._servers = dict(servers)
        self._hooks = ConnectionParamHooks(types_ref)
        self._schemas = ToolSchema()
        self._params: dict[str, dict[str, ConnectionParam]] = {}
        self._lists: dict[str, tuple[str, ...]] = {}
        self._profiles: dict[str, ConnectionBase] = {}
        for name, server in self._servers.items():
            raw = {
                **server.connection,
                "kind": server.kind,
                "description": server.description,
            }
            try:
                parsed = types_ref().parse(raw)
            except ConnectionTypesError as exc:
                msg = (
                    f"[connections.{name}]: the profile does not parse as a "
                    f"connection of kind {server.kind!r}: {exc}"
                )
                raise ToolConfigError(msg) from exc

            self._profiles[name] = parsed.identified(UUID(int=0), name)

    def listed(self, lists: Mapping[str, Sequence[str]]) -> None:
        """Списки заданных соединений сервера у инструментов: имя инструмента
        → имена из [connections]. Зовётся до сборки инструментов.

        Ошибки:
        ToolConfigError — список называет соединение, которого нет в
            [connections].
        """
        for tool, names in lists.items():
            for name in names:
                if name in self._profiles:
                    continue

                msg = (
                    f"the server connections of tool {tool!r} name {name!r}, "
                    f"which is not among [connections]: {sorted(self._profiles)}"
                )
                raise ToolConfigError(msg)

            self._lists[tool] = tuple(names)

    def bind_all(self, tools: Sequence[HostedTool]) -> None:
        self._hooks.bind_all(tools, self._hook)

    def offered(
        self,
        tools: Sequence[HostedTool],
        roles: frozenset[str],
        sources: frozenset[OfferedSource],
    ) -> Sequence[HostedTool]:
        """Инструменты под предложение вызывающему с ролями roles и
        источниками sources: инструмент без параметров-соединений — как
        есть; с ними — копия, у которой схема параметра по предложению и
        значение на вызове проверяется по нему же. Параметру, которому
        предложить нечего, инструмента в списке нет."""
        stored = OfferedSource.STORED in sources
        offered: list[HostedTool] = []
        for tool in tools:
            params = self._params.get(tool.name)
            if not params:
                offered.append(tool)
                continue

            names: Mapping[str, str] = {}
            if OfferedSource.SERVER in sources:
                names = self._visible(tool.name, roles)

            has_names = bool(names)
            available = any((stored, has_names))
            if not available:
                continue

            fields: dict[str, tuple[Any, FieldInfo]] = {}
            for param, hook in params.items():
                offer = ConnectionOffer(kind=hook.kind, names=names, stored=stored)
                checked = AfterValidator(partial(self._checked, offer))
                fields[param] = self._hooks.field(offer, checked)

            copy = tool.copy()
            copy.args_schema = self._schemas.rebuild(tool.args_schema, fields, ())
            offered.append(copy)

        return offered

    async def check(self, tool: str, args: Mapping[str, object]) -> None:
        for param, hook in self._params.get(tool, {}).items():
            await hook.verified(tool, args.get(param))

    def _visible(self, tool: str, roles: frozenset[str]) -> dict[str, str]:
        """Заданные соединения из списка инструмента tool, видимые ролям
        roles: имя → описание для модели."""
        visible: dict[str, str] = {}
        for name in self._lists.get(tool, ()):
            if not self._servers[name].visible_for(roles):
                continue

            visible[name] = self._profiles[name].description

        return visible

    def _checked(self, offer: ConnectionOffer, value: str) -> str:
        """Проверяет источник имени до обращения к каталогу или конфигу."""
        if value in offer.names:
            return value

        if offer.stored:
            if value in self._profiles:
                msg = "the configured server connection is not offered to this caller"
                raise ValueError(msg)

            return value

        names = ", ".join(offer.names)
        msg = (
            f"the connection name is not available to this caller; use one of: {names}"
        )
        raise ValueError(msg)

    def _hook(self, tool: str, param: str, kind: str) -> AsyncInjected:
        """Обвязка параметра param инструмента tool вида kind; соединение
        чужого вида в списке инструмента — ToolConfigError."""
        listed: dict[str, ConnectionBase] = {}
        for name in self._lists.get(tool, ()):
            profile = self._profiles[name]
            if profile.kind != kind:
                msg = (
                    f"tool {tool!r}: {param} takes connections of kind {kind!r}, "
                    f"but server connection {name!r} in its list is of kind "
                    f"{profile.kind!r}"
                )
                raise ToolConfigError(msg)

            listed[name] = profile

        whitelist = ConnectionWhitelist(connections=listed, ambiguous=frozenset())
        hook = ConnectionParam(
            self._contexts,
            self._tickets,
            whitelist,
            param,
            kind,
            self._servers,
            self._stored,
        )
        self._params.setdefault(tool, {})[param] = hook

        return hook
