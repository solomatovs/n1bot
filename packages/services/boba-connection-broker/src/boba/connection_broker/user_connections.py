"""Соединения пользователя: параметр инструмента и профиль по имени.

Инструмент объявляет соединение параметром `Annotated[<Профиль>, UserConnection]`;
манифест плагина называет такой параметр и вид соединения. Модель видит на
этом месте строку. Здесь две части пути соединения:

- ConnectionParamHooks ставит на такие параметры обвязку, которая отдаст телу
  профиль, а в схеме инструмента делает параметр строкой с пометкой вида и
  запечатанным профилем (SealedParamSchema). Откуда обвязка берёт профиль, решает
  её владелец (запечатанное значение клиента — boba.connection_broker.sealed).
- ArmedConnections — сторона клиента: по виду и имени ищет соединение среди
  выданных субъекту вызова и заменяет kerberos-секцию билетом этого вызова.
  Готовый профиль клиент запечатывает и отправляет серверу инструментов.

Ошибки:
RefusalError — вызов вне сессии, соединение не выдано субъекту, выдано дважды
    либо делегированных кредов у сессии нет; kind из ConnectionRefusal.
ConnectionStoreError — таблица соединений недоступна.
KerberosError — билет к соединению не выпущен, вызов начинать нечем.
ToolConfigError — параметр объявлен непригодной моделью либо строка таблицы
    несёт готовый билет.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from pydantic import AfterValidator
from pydantic.fields import FieldInfo

from boba.connection_broker.store import ConnectionStore
from boba.connection_broker.tickets import CredentialsRef
from boba.connections.base import ConnectionBase
from boba.connections.manifest import ConnectionTypes, UnknownConnectionKindError
from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import (
    ConnectionOffer,
    SealedConnection,
    SealedParamSchema,
)
from boba.connections.whitelist import (
    AmbiguousConnectionError,
    ConnectionWhitelist,
)
from boba.core.secrets import SecretReveal
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
    """Постановка обвязок на параметры-соединения инструментов.

    Манифест плагина называет параметры-соединения инструмента и их вид.
    Здесь каждый такой параметр получает обвязку, которая отдаст телу
    профиль, а в схеме инструмента становится строкой с ключами
    SealedParamSchema: пометкой вида, по которой клиент узнаёт параметр, и
    веткой запечатанного значения. Значение в лог не пишется: там
    запечатанное соединение. Вид, которого сервер не знает, — отказ сборки:
    виды соединений — код сервера. Откуда обвязка возьмёт профиль, решает
    вызывающий фабрикой make (SealedConnectionParams).
    """

    def __init__(self, types_ref: TypesRef) -> None:
        self._types_ref = types_ref
        self._bodies = ToolBody()
        self._schemas = ToolSchema()
        self._declared = SealedParamSchema()

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
            shown[param] = self.field(ConnectionOffer(kind=kind, sealed=True, names={}))

            logger.info(
                "tool %s: %s is a %s connection of the caller", tool.name, param, kind
            )

        tool.args_schema = self._schemas.rebuild(tool.args_schema, shown, ())

    def field(
        self, offer: ConnectionOffer, *checks: AfterValidator
    ) -> tuple[Any, FieldInfo]:
        """Поле параметра-соединения по предложению offer: строка, которая в
        лог не пишется, со схемой SealedParamSchema и проверками checks."""
        marked = Annotated[str, NotLogged]
        declared = self._declared.declared(offer)
        info = FieldInfo(min_length=1, json_schema_extra=declared)
        info.metadata.extend(checks)

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
    """Соединение пользователя по виду и имени, готовое к отправке серверу.

    Сторона клиента: соединения и гранты хранит чат. По имени ищет строку
    нужного вида среди выданных субъекту вызова, заменяет её kerberos-секцию билетом
    этого вызова и отдаёт содержимое для запечатывания. Пользуется им
    запечатывание перед отправкой вызова (boba.connection_broker.sealing);
    субъект и креды берутся из контекста вызова.
    """

    def __init__(
        self,
        store_ref: StoreRef,
        credentials_ref: CredentialsRef,
        contexts: CallContexts,
    ) -> None:
        self._store_ref = store_ref
        self._credentials_ref = credentials_ref
        self._contexts = contexts

    async def sealed(self, kind: str, name: str, ttl: timedelta) -> SealedConnection:
        """Соединение субъекта вида kind с именем name, готовое к
        запечатыванию: профиль с кредами этого вызова, имя и срок годности."""
        context = self._contexts.current()
        rows = await self._store_ref().for_subject(context.subject, kind)

        connection = self._pick(ConnectionWhitelist.of(rows), kind, name)
        self._require_stored_section(connection, kind, name)

        armed = await self._credentials_ref().for_connection(
            connection, context.credential
        )

        logger.info("connection %s of kind %s armed: %s", name, kind, armed.trace())

        return SealedConnection(
            name=name,
            expires_at=datetime.now(UTC) + ttl,
            profile=SecretReveal.dumped(armed),
        )

    @staticmethod
    def _pick(whitelist: ConnectionWhitelist, kind: str, name: str) -> ConnectionBase:
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

        known = ", ".join(whitelist.names())
        if not known:
            known = "none"

        # значение не цитируется: вместо имени модель могла написать что
        # угодно, вплоть до строки подключения с паролем
        msg = (
            f"the connection named by {len(name)} characters is not among "
            f"the connections of kind {kind!r} available to you; yours are: {known}"
        )
        raise RefusalError(ConnectionRefusal.NOT_VISIBLE, msg)

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
