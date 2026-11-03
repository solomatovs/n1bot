"""Серверный каталог connections и обвязки параметров инструментов.

Ошибки:
RefusalError — имя недоступно субъекту, неоднозначно или нет credentials.
ConnectionStoreError — таблица connections недоступна.
KerberosError — не удалось подготовить билет вызова.
ToolConfigError — неизвестный вид параметра или готовый билет в строке БД."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Annotated, Any

from pydantic.fields import FieldInfo

from boba.access.grants import ConnectionFilter
from boba.connection_broker.store import ConnectionStore
from boba.connection_broker.tickets import CredentialsRef
from boba.connections.base import ConnectionBase
from boba.connections.manifest import ConnectionTypes, UnknownConnectionKindError
from boba.connections.marks import ConnectionRefusal
from boba.connections.offers import (
    ConnectionOffer,
    ConnectionParamSchema,
)
from boba.connections.whitelist import (
    AmbiguousConnectionError,
    ConnectionWhitelist,
)
from boba.identity.context import CallContexts
from boba.identity.errors import RefusalError
from boba.kerberos import TicketAuth
from boba.toolkit.facade import NotLogged
from boba.toolrun.hosted import HostedTool, ToolSchema
from boba.toolrun.injected import AsyncInjected, ToolConfigError
from boba.toolrun.wrapping import ToolBody

__all__ = [
    "ArmedConnections",
    "ConnectionParamHooks",
    "ConnectionRefusal",
    "CredentialsRef",
    "StoreRef",
    "TypesRef",
]

logger = logging.getLogger(__name__)


StoreRef = Callable[[], ConnectionStore]
"""Хранилище соединений; зовётся на вызов, а не при загрузке инструментов."""

TypesRef = Callable[[], ConnectionTypes]
"""Реестр установленных типов; по нему модель профиля превращается в kind."""


HookFactory = Callable[[str, str, str], AsyncInjected]
"""Обвязка параметра-соединения по имени инструмента, имени параметра и
виду соединения."""


class ConnectionParamHooks:
    """Ставит обвязки параметров из манифеста и публикует строковые схемы имён.

    ConnectionParams передаёт фабрику resolver-обвязок; тело получает профиль."""

    def __init__(self, types_ref: TypesRef) -> None:
        self._types_ref = types_ref
        self._bodies = ToolBody()
        self._schemas = ToolSchema()
        self._declared = ConnectionParamSchema()

    def bind_all(self, tools: Sequence[HostedTool], make: HookFactory) -> None:
        """Обвязки параметров-соединений каждого инструмента плагина."""
        for tool in tools:
            self._bind_one(tool, make)

    def _bind_one(self, tool: HostedTool, make: HookFactory) -> None:
        declared = tool.inputs.connections
        if not declared:
            return

        shown: dict[str, tuple[Any, FieldInfo]] = {}
        for param, kind in declared.items():
            self._require_kind(tool.name, param, kind)

            self._bodies.hook_all([tool], make(tool.name, param, kind))
            shown[param] = self.field(kind)

            logger.info(
                "tool %s: %s is a %s connection of the caller", tool.name, param, kind
            )

        tool.args_schema = self._schemas.rebuild(tool.args_schema, shown, ())

    def field(self, kind: str) -> tuple[Any, FieldInfo]:
        """Строковое поле имени с видом connection и подсказкой каталога."""
        marked = Annotated[str, NotLogged]
        declared = self._declared.declared(ConnectionOffer(kind))
        info = FieldInfo(min_length=1, json_schema_extra=declared)

        return marked, info

    def _require_kind(self, tool: str, param: str, kind: str) -> None:
        """Вид соединения из манифеста известен серверу: вид — код сервера."""
        try:
            self._types_ref().manifest_of(kind)
        except UnknownConnectionKindError as exc:
            msg = (
                f"tool {tool!r}: {param} needs connection type {kind!r}, which "
                f"this server does not know: {exc}"
            )
            raise ToolConfigError(msg) from exc


class ArmedConnections:
    """Читает профиль по имени и grants, готовит credentials в контексте MCP.

    Создаётся сборкой сервера из хранилища, источника билетов и CallContexts."""

    def __init__(
        self,
        store_ref: StoreRef,
        credentials_ref: CredentialsRef,
        contexts: CallContexts,
    ) -> None:
        self._store_ref = store_ref
        self._credentials_ref = credentials_ref
        self._contexts = contexts

    async def available(self, kind: str, name: str) -> ConnectionBase:
        """Профиль из хранилища по текущим грантам без подготовки билетов."""
        subject = self._contexts.subject()
        store = self._store_ref()
        rows = await store.for_subject(subject, kind, name=name)
        if not rows:
            catalog = await store.catalog(subject, ConnectionFilter(kind=kind))
            names = sorted(row.name for row in catalog)
            known = ", ".join(names)
            if not known:
                known = "none"

            raise self._missing(kind, name, known)

        connection = self._pick(ConnectionWhitelist.of(rows), kind, name)
        self._require_stored_section(connection, kind, name)
        return connection

    async def armed(self, connection: ConnectionBase) -> ConnectionBase:
        """Подготовить credentials в контексте исполнителя, без передачи клиенту."""
        credential = self._contexts.current().credential
        return await self._credentials_ref().for_connection(connection, credential)

    def _pick(
        self, whitelist: ConnectionWhitelist, kind: str, name: str
    ) -> ConnectionBase:
        try:
            picked = whitelist.pick(name)
        except AmbiguousConnectionError as exc:
            msg = (
                f"connection {name!r} matches more than one of your "
                f"connections: {exc}; ask the administrator to resolve the overlap"
            )
            raise RefusalError(ConnectionRefusal.AMBIGUOUS, msg) from exc

        if picked is not None:
            return picked

        raise self._missing(kind, name, "none")

    @staticmethod
    def _missing(kind: str, name: str, known: str) -> RefusalError:
        # Вместо имени может прийти строка с паролем: её не цитируем.
        msg = (
            f"the connection named by {len(name)} characters is not among "
            f"the connections of kind {kind!r} available to you; yours are: {known}"
        )
        return RefusalError(ConnectionRefusal.NOT_VISIBLE, msg)

    @staticmethod
    def _require_stored_section(
        connection: ConnectionBase, kind: str, name: str
    ) -> None:
        """В таблице лежат только делегированные и keytab-секции."""
        section = connection.kerberos_section()
        if not isinstance(section, TicketAuth):
            return

        msg = (
            f"stored connection {name!r} of kind {kind!r} carries a "
            "ticket kerberos section: only delegated or keytab credentials "
            "are allowed in the table"
        )
        raise ToolConfigError(msg)
