"""Профиль соединения в параметр инструмента перед каждым вызовом.

Инструмент объявляет соединение параметром `Annotated[<Профиль>, UserConnection]`.
Модель видит на этом месте строку — имя соединения; хост по типу параметра
узнаёт вид, ищет строку среди выданных субъекту вызова, заменяет kerberos-секцию
билетом этого вызова и подставляет готовый профиль. Тело получает профиль и про
пользователя, гранты и билеты не знает.

Ошибки:
RefusalError — вызов вне сессии, имя не выдано субъекту, выдано дважды либо
    делегированных кредов у сессии нет; kind из ConnectionRefusal.
ConnectionStoreError — таблица соединений недоступна.
KerberosError — билет к соединению не выпущен, вызов начинать нечем.
ToolConfigError — параметр объявлен непригодной моделью либо строка таблицы
    несёт готовый билет.
InjectedAsyncOnlyError — тело инструмента вызвано синхронно: профиль
    подставляется только в async-теле.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Any, ClassVar

from langchain_core.tools import BaseTool
from pydantic.fields import FieldInfo

from boba.connection_broker.store import ConnectionStore
from boba.connection_broker.tickets import CredentialsRef
from boba.connections.base import ClientIdentity, ConnectionProfileBase
from boba.connections.credentials import ProfileSections
from boba.connections.manifest import ConnectionTypes, UnknownConnectionKindError
from boba.connections.marks import ConnectionRefusal
from boba.connections.whitelist import AmbiguousConnectionError, ConnectionWhitelist
from boba.identity.context import CallContext
from boba.identity.errors import RefusalError
from boba.kerberos import TicketAuth
from boba.toolkit.calls import ConnectionArg
from boba.toolkit.entry import ToolArgv
from boba.toolrun.injected import AsyncInjected, ToolConfigError
from boba.toolrun.wrapping import ToolBody, ToolSchema

__all__ = [
    "ConnectionRefusal",
    "CredentialsRef",
    "StoreRef",
    "TypesRef",
    "UserConnections",
]

logger = logging.getLogger(__name__)


class CallerApplication:
    """Имя приложения в подписи сессии: под ним ходят все инструменты."""

    NAME: ClassVar[str] = "boba"

StoreRef = Callable[[], ConnectionStore]
"""Хранилище соединений; зовётся на вызов, а не при загрузке инструментов."""

TypesRef = Callable[[], ConnectionTypes]
"""Реестр установленных типов; по нему модель профиля превращается в kind."""


class ConnectionArgument:
    """Поле, которым параметр-соединение показывается модели: имя строки.

    Вид соединения едет метадатой ConnectionArg: по ней страницы workflow
    рисуют выбор из соединений нужного вида, а не поле для ввода текста.
    """

    DESCRIPTION: ClassVar[str] = (
        "Имя соединения из connection_list. Бери имя строки, чей kind подходит "
        "инструменту, а описание — задаче пользователя."
    )

    @classmethod
    def field(cls, kind: str) -> tuple[Any, FieldInfo]:
        annotation = Annotated[str, ConnectionArg(family=kind)]

        return annotation, FieldInfo(min_length=1, description=cls.DESCRIPTION)


class UserConnections(AsyncInjected):
    """Обвязка одного параметра-соединения: имя от модели, профиль от хоста."""

    def __init__(
        self,
        store_ref: StoreRef,
        credentials_ref: CredentialsRef,
        types_ref: TypesRef,
        param: str,
        kind: str,
    ) -> None:
        super().__init__(param, None)
        self._store_ref = store_ref
        self._credentials_ref = credentials_ref
        self._types_ref = types_ref
        self._kind = kind

    @classmethod
    def bind_all(
        cls,
        tools: Sequence[BaseTool],
        store_ref: StoreRef,
        credentials_ref: CredentialsRef,
        types_ref: TypesRef,
    ) -> None:
        """Ставит обвязку на каждый параметр-соединение и правит схему для LLM.

        Зовётся до InjectedConfig: параметры читаются со схемы, пока она полная.
        Вид соединения берётся из типа параметра — реестр знает, какому пакету
        принадлежит модель профиля.
        """
        for tool in tools:
            cls._bind_one(tool, store_ref, credentials_ref, types_ref)

    @classmethod
    def _bind_one(
        cls,
        tool: BaseTool,
        store_ref: StoreRef,
        credentials_ref: CredentialsRef,
        types_ref: TypesRef,
    ) -> None:
        schema = ToolSchema.of(tool)
        if schema is None:
            return

        fields = ToolArgv.connection_fields(schema)
        if not fields:
            return

        shown: dict[str, tuple[Any, FieldInfo]] = {}
        for param, annotation in fields.items():
            kind = cls._kind_of(tool.name, param, annotation, types_ref)

            ToolBody.hook_all(
                [tool], cls(store_ref, credentials_ref, types_ref, param, kind)
            )
            shown[param] = ConnectionArgument.field(kind)

            logger.info(
                "tool %s: %s is a %s connection of the caller", tool.name, param, kind
            )

        tool.args_schema = ToolSchema.rebuild(schema, shown, ())

    @staticmethod
    def _kind_of(tool: str, param: str, annotation: object, types_ref: TypesRef) -> str:
        """Вид соединения по модели профиля параметра."""
        if not isinstance(annotation, type):
            msg = f"tool {tool!r}: {param} is not annotated with a profile model"
            raise ToolConfigError(msg)

        if not issubclass(annotation, ConnectionProfileBase):
            msg = (
                f"tool {tool!r}: {param} is annotated with {annotation.__name__}, "
                "which is not a connection profile"
            )
            raise ToolConfigError(msg)

        try:
            return types_ref().kind_of(annotation)
        except UnknownConnectionKindError as exc:
            msg = (
                f"tool {tool!r}: {param} needs connection type "
                f"{annotation.__name__}, whose package is not installed"
            )
            raise ToolConfigError(msg) from exc

    async def value(self, name: str, kwargs: dict[str, object]) -> object:
        requested = self._requested(name, kwargs)

        subject = CallContext.current().subject
        rows = await self._store_ref().for_subject(subject, self._kind)
        whitelist = ConnectionWhitelist.of(rows)

        picked = self._pick(whitelist, requested)
        profile = self._labelled(picked.profile, name)
        armed = await self._armed(profile)

        logger.info(
            "tool %s: connection %r (%s) %s",
            name,
            picked.name,
            self._kind,
            armed.trace(),
        )

        return armed

    def _requested(self, tool: str, kwargs: Mapping[str, object]) -> str:
        value = kwargs.get(self._param)
        if isinstance(value, str) and value:
            return value

        msg = (
            f"{tool} needs a connection name in {self._param!r}; "
            "call connection_list to see the names available to you"
        )
        raise RefusalError(ConnectionRefusal.NOT_VISIBLE, msg)

    def _pick(self, whitelist: ConnectionWhitelist, requested: str):
        try:
            picked = whitelist.pick(requested)
        except AmbiguousConnectionError as exc:
            msg = (
                f"connection {requested!r} matches more than one of your "
                "connections; ask the administrator to resolve the overlap"
            )
            raise RefusalError(ConnectionRefusal.AMBIGUOUS, msg) from exc

        if picked is not None:
            return picked

        known = ", ".join(whitelist.names())
        msg = (
            f"connection {requested!r} of kind {self._kind!r} is not available "
            f"to you; yours are: {known or 'none'}"
        )
        raise RefusalError(ConnectionRefusal.NOT_VISIBLE, msg)

    @staticmethod
    def _labelled(profile: ConnectionProfileBase, tool: str) -> ConnectionProfileBase:
        """Профиль, подписанный клиентом вызова; как подписать, решает профиль."""
        login = CallContext.current().subject.login
        client = ClientIdentity(
            application=CallerApplication.NAME, login=login, tool=tool
        )

        return profile.labeled(client)

    async def _armed(self, profile: ConnectionProfileBase) -> ConnectionProfileBase:
        """Профиль с билетом вызова вместо kerberos-секции строки."""
        section = ProfileSections.section_of(profile)
        if isinstance(section, TicketAuth):
            msg = (
                "stored connection carries a ticket kerberos section: "
                "only delegated or keytab credentials are allowed in the table"
            )
            raise ToolConfigError(msg)

        credential = CallContext.current().credential

        return await self._credentials_ref().for_connection(profile, credential)
