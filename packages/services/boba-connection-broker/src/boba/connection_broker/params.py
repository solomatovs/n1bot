"""Разрешение имён connections перед исполнением инструментов MCP.

Ошибки:
RefusalError — имя недоступно субъекту, неоднозначно или нет credentials.
ToolConfigError — некорректное объявление параметра или профиль.
KerberosError — не удалось подготовить билет вызова.
ConnectionStoreError — каталог MCP недоступен."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from pydantic.fields import FieldInfo

from boba.connection_broker.user_connections import (
    ArmedConnections,
    ConnectionParamHooks,
    TypesRef,
)
from boba.connections.base import ClientIdentity, ConnectionBase
from boba.connections.marks import ConnectionRefusal
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

__all__ = ["ConnectionParam", "ConnectionParams"]

logger = logging.getLogger(__name__)


class ConnectionParam(AsyncInjected):
    """Подставляет профиль вместо имени параметра, затем готовит credentials.

    Создаётся ConnectionParams; grants проверяет ArmedConnections в контексте вызова."""

    APPLICATION: ClassVar[str] = "boba"
    """Имя приложения в подписи профиля: под ним ходят все инструменты."""

    def __init__(
        self,
        contexts: CallContexts,
        tickets: ConfigArming,
        param: str,
        kind: str,
        stored: ArmedConnections | None = None,
    ) -> None:
        super().__init__(param, None)
        self._contexts = contexts
        self._tickets = tickets
        self.kind = kind
        self._stored = stored

    async def value(self, name: str, kwargs: dict[str, object]) -> object:
        value = kwargs.get(self._param)
        connection = await self.verified(name, value)
        resolver = self._stored
        if resolver is None:
            msg = f"{name}: the connection catalog has no credential resolver"
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

    async def verified(self, name: str, value: object) -> ConnectionBase:
        """Проверяет имя и текущие права до выдачи credentials и запуска тела."""
        if not isinstance(value, str):
            msg = (
                f"{name}: {self._param!r} expects a connection name, "
                f"got {type(value).__name__}"
            )
            raise RefusalError(ConnectionRefusal.NOT_VISIBLE, msg)

        resolver = self._stored
        if resolver is None:
            msg = f"{name}: the connection catalog is unavailable"
            raise RefusalError(ConnectionRefusal.NOT_VISIBLE, msg)

        return await resolver.available(self.kind, value)


class ConnectionParams(NodeArgs, ParamSource):
    """Ставит серверные resolver-обвязки и публикует схемы имён connections.

    Собирается runtime. Реализует ParamSource и NodeArgs: проверяет права
    на профили при вызове инструмента и до запуска DAG."""

    def __init__(
        self,
        types_ref: TypesRef,
        contexts: CallContexts,
        tickets: ConfigArming,
        stored: ArmedConnections | None = None,
    ) -> None:
        self._types_ref = types_ref
        self._contexts = contexts
        self._tickets = tickets
        self._stored = stored
        self._hooks = ConnectionParamHooks(types_ref)
        self._schemas = ToolSchema()
        self._params: dict[str, dict[str, ConnectionParam]] = {}

    def bind_all(self, tools: Sequence[HostedTool]) -> None:
        self._hooks.bind_all(tools, self._hook)

    def published(self, tools: Sequence[HostedTool]) -> Sequence[HostedTool]:
        """Схемы с именами connections; доступ к каждой записи проверяется на вызове."""
        offered: list[HostedTool] = []
        for tool in tools:
            params = self._params.get(tool.name)
            if not params:
                offered.append(tool)
                continue

            fields: dict[str, tuple[Any, FieldInfo]] = {}
            for param, hook in params.items():
                fields[param] = self._hooks.field(hook.kind)

            copy = tool.copy()
            copy.args_schema = self._schemas.rebuild(tool.args_schema, fields, ())
            offered.append(copy)

        return offered

    async def check(self, tool: str, args: Mapping[str, object]) -> None:
        for param, hook in self._params.get(tool, {}).items():
            await hook.verified(tool, args.get(param))

    def _hook(self, tool: str, param: str, kind: str) -> AsyncInjected:
        """Обвязка параметра param инструмента tool вида kind."""
        hook = ConnectionParam(
            self._contexts,
            self._tickets,
            param,
            kind,
            self._stored,
        )
        self._params.setdefault(tool, {})[param] = hook

        return hook
