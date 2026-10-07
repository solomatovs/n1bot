"""Соединения пользователя: параметр инструмента и профиль по ссылке.

Инструмент объявляет соединение параметром `Annotated[<Профиль>, UserConnection]`.
Модель видит на этом месте строку. Здесь две части пути соединения:

- ConnectionParamHooks ставит на такие параметры обвязку, которая отдаст телу
  профиль, а в схеме для LLM делает параметр строкой с меткой вида
  соединения. Откуда обвязка берёт профиль, решает её владелец
  (запечатанное значение клиента — boba.connection_broker.sealed).
- ArmedConnections — сторона клиента: по ссылке модели ищет соединение среди
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

from pydantic.fields import FieldInfo

from boba.connection_broker.store import ConnectionStore
from boba.connection_broker.tickets import CredentialsRef
from boba.connections.base import ConnectionBase
from boba.connections.manifest import ConnectionTypes, UnknownConnectionKindError
from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import (
    ConnectionRef,
    SealedConnection,
)
from boba.connections.whitelist import (
    AmbiguousConnectionError,
    ConnectionWhitelist,
)
from boba.identity.context import CallContexts
from boba.identity.errors import RefusalError
from boba.kerberos import TicketAuth
from boba.toolkit.entry import ToolArgv
from boba.toolkit.facade import NotLogged
from boba.toolkit.types import SecretReveal
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

    Инструмент объявляет соединение параметром с моделью профиля. Здесь
    каждый такой параметр получает обвязку, которая отдаст телу профиль, а в
    схеме для LLM становится строкой с описанием description, где назван
    вид соединения ({kind}). Значение в лог не пишется: там запечатанное
    соединение. Вид берётся из типа параметра — реестр знает, какому пакету
    принадлежит модель профиля. Откуда обвязка возьмёт
    профиль, решает вызывающий фабрикой make (SealedConnectionParams).
    """

    def __init__(self, types_ref: TypesRef, description: str) -> None:
        self._types_ref = types_ref
        self._description = description
        self._bodies = ToolBody()
        self._schemas = ToolSchema()

    def bind_all(self, tools: Sequence[HostedTool], make: HookFactory) -> None:
        """Зовётся до InjectedConfig: параметры читаются со схемы, пока она
        полная."""
        for tool in tools:
            self._bind_one(tool, make)

    def _bind_one(self, tool: HostedTool, make: HookFactory) -> None:
        schema = tool.args_schema
        fields = ToolArgv.connection_fields(schema)
        if not fields:
            return

        shown: dict[str, tuple[Any, FieldInfo]] = {}
        for param, annotation in fields.items():
            kind = self._kind_of(tool.name, param, annotation)

            self._bodies.hook_all([tool], make(tool.name, param, kind))
            shown[param] = self._field(kind)

            logger.info(
                "tool %s: %s is a %s connection of the caller", tool.name, param, kind
            )

        tool.args_schema = self._schemas.rebuild(schema, shown, ())

    def _field(self, kind: str) -> tuple[Any, FieldInfo]:
        marked = Annotated[str, NotLogged]

        described = self._description.format(kind=kind)

        return marked, FieldInfo(min_length=1, description=described)

    def _kind_of(self, tool: str, param: str, annotation: object) -> str:
        """Вид соединения по модели профиля параметра."""
        if not isinstance(annotation, type):
            msg = (
                f"tool {tool!r}: {param} must be annotated with a connection "
                f"model, got {annotation!r}"
            )
            raise ToolConfigError(msg)

        if not issubclass(annotation, ConnectionBase):
            msg = (
                f"tool {tool!r}: {param} is annotated with {annotation.__name__}, "
                "which is not a connection model"
            )
            raise ToolConfigError(msg)

        try:
            return self._types_ref().kind_of(annotation)
        except UnknownConnectionKindError as exc:
            msg = (
                f"tool {tool!r}: {param} needs connection type "
                f"{annotation.__name__}, whose package is not installed: {exc}"
            )
            raise ToolConfigError(msg) from exc


class ArmedConnections:
    """Соединение пользователя по ссылке модели, готовое к отправке серверу.

    Сторона клиента: соединения и гранты хранит чат. По ссылке ищет строку
    среди выданных субъекту вызова, заменяет её kerberos-секцию билетом
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

    async def sealed(self, ref: ConnectionRef, ttl: timedelta) -> SealedConnection:
        """Соединение субъекта по ссылке ref, готовое к запечатыванию: профиль
        с кредами этого вызова, сама ссылка, логин субъекта и срок годности."""
        context = self._contexts.current()
        rows = await self._store_ref().for_subject(context.subject, ref.kind)

        connection = self._pick(ConnectionWhitelist.of(rows), ref)
        self._require_stored_section(connection, ref)

        armed = await self._credentials_ref().for_connection(
            connection, context.credential
        )

        logger.info("connection %s armed: %s", ref.render(), armed.trace())

        return SealedConnection(
            ref=ref.render(),
            login=context.subject.login,
            expires_at=datetime.now(UTC) + ttl,
            profile=SecretReveal.dumped(armed),
        )

    @staticmethod
    def _pick(whitelist: ConnectionWhitelist, ref: ConnectionRef) -> ConnectionBase:
        try:
            picked = whitelist.pick(ref.name)
        except AmbiguousConnectionError as exc:
            msg = (
                f"connection {ref.name!r} matches more than one of your "
                f"connections: {exc}; ask the administrator to resolve the overlap"
            )
            raise RefusalError(ConnectionRefusal.AMBIGUOUS, msg) from exc

        if picked is not None:
            return picked

        known = ", ".join(whitelist.names())
        if not known:
            known = "none"

        msg = (
            f"connection {ref.name!r} of kind {ref.kind!r} is not available "
            f"to you; yours are: {known}"
        )
        raise RefusalError(ConnectionRefusal.NOT_VISIBLE, msg)

    @staticmethod
    def _require_stored_section(connection: ConnectionBase, ref: ConnectionRef) -> None:
        """В таблице лежат только делегированные и keytab-секции."""
        section = connection.kerberos_section()
        if not isinstance(section, TicketAuth):
            return

        msg = (
            f"stored connection {ref.name!r} of kind {ref.kind!r} carries a "
            "ticket kerberos section: only delegated or keytab credentials "
            "are allowed in the table"
        )
        raise ToolConfigError(msg)
