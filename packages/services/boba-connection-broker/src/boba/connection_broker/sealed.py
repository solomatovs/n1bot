"""Соединения инструментов на стороне исполнителя: запечатанные значения
клиента и заданные соединения сервера.

Соединений пользователей исполнитель не хранит. Клиент присылает профиль на
месте параметра-соединения запечатанным открытым ключом исполнителя
(boba.connections.sealed); ключ клиент узнаёт из возможностей сервера при
подключении. Заданные соединения сервера (ServerConnection, секция
[connections.<имя>]) описаны в его конфиге со своими ролями, у инструмента —
список их имён. SealedConnectionParams ставит на параметры-соединения обвязку
SealedConnectionParam: имя из списка инструмента она берёт заданным
соединением под сервисными кредами, иначе открывает запечатанное значение,
проверяет срок и вид и отдаёт телу готовый профиль. Он же — правило
аргументов узла (NodeArgs): исполнитель проверяет значения всех узлов до
старта DAG; он же строит копии инструментов под предложение вызывающему
(offered) и объявляет возможность сервера с ключом.

Ошибки:
RefusalError — значение не запечатано, запечатано другим ключом, повреждено,
    просрочено либо несёт соединение другого вида; kind из
    ConnectionRefusal.
ToolConfigError — параметр объявлен непригодной моделью соединения, заданное
    соединение сервера не разбирается профилем своего вида, список
    инструмента называет незаданное соединение либо соединение чужого вида.
KerberosError — билет к заданному соединению сервера не выпущен.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from functools import partial
from typing import Any, ClassVar
from uuid import UUID

from pydantic import AfterValidator, ConfigDict, Field
from pydantic.fields import FieldInfo

from boba.access import RoleVisibility
from boba.connection_broker.user_connections import (
    ConnectionParamHooks,
    TypesRef,
)
from boba.connections.base import ClientIdentity, ConnectionBase
from boba.connections.manifest import ConnectionTypesError
from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import (
    ConnectionOffer,
    OfferedSource,
    SealedConnection,
    SealFeature,
    SealKeys,
)
from boba.connections.whitelist import ConnectionWhitelist
from boba.identity.context import CallContexts
from boba.identity.errors import RefusalError
from boba.toolkit.types import StringList
from boba.toolrun.hosted import HostedTool, ToolSchema
from boba.toolrun.injected import (
    AsyncInjected,
    ConfigArming,
    ParamSource,
    ToolConfigError,
)
from boba.toolrun.stream_calls import NodeArgs

__all__ = ["SealedConnectionParam", "SealedConnectionParams", "ServerConnection"]

logger = logging.getLogger(__name__)


class ServerConnection(RoleVisibility):
    """Заданное соединение сервера, секция [connections.<имя>]: вид, описание
    для модели, роли, которым оно видно, и профиль под сервисной учёткой
    (keytab либо пароль из секретов site).

    Конфиг сервиса boba-mcp разбирает секцию этой моделью на старте;
    SealedConnectionParams один раз собирает из неё профиль вида kind и
    дальше держит только его. Делегирования у таких соединений нет.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    description: str = Field(min_length=1)
    roles: StringList = Field(default=[], min_length=1, validate_default=True)
    connection: dict[str, Any]


class SealedConnectionParam(AsyncInjected):
    """Обвязка одного параметра-соединения на стороне исполнителя.

    Имя заданного соединения сервера из списка инструмента (listed) она
    берёт профилем этого соединения; иначе на месте параметра запечатанный
    профиль клиента: обвязка открывает его ключом исполнителя, проверяет срок
    годности и вид соединения и собирает профиль по реестру типов. Профиль
    подписывается клиентом вызова; keytab-секция заданного соединения
    заменяется билетом вызова (tickets). Тело получает готовый профиль.
    Ставит её SealedConnectionParams.
    """

    APPLICATION: ClassVar[str] = "boba"
    """Имя приложения в подписи профиля: под ним ходят все инструменты."""

    RESEAL_HINT: ClassVar[str] = (
        "repeat the call with the connection name instead of the sealed value"
    )

    def __init__(  # noqa: PLR0913 — обвязка собирается всеми входами параметра
        self,
        keys: SealKeys,
        types_ref: TypesRef,
        contexts: CallContexts,
        tickets: ConfigArming,
        listed: ConnectionWhitelist,
        param: str,
        kind: str,
    ) -> None:
        super().__init__(param, None)
        self._keys = keys
        self._contexts = contexts
        self._types_ref = types_ref
        self._tickets = tickets
        self._listed = listed
        self.kind = kind

    async def value(self, name: str, kwargs: dict[str, object]) -> object:
        connection = self.verified(name, kwargs.get(self._param))
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

    def verified(self, name: str, value: object) -> ConnectionBase:
        """Профиль по значению: имя заданного соединения сервера из списка
        инструмента — его профиль; иначе запечатанное значение, у которого
        срок не вышел и вид тот."""
        server: ConnectionBase | None = None
        if isinstance(value, str):
            server = self._listed.pick(value)

        if server is not None:
            return server

        sealed = self._opened(name, value)

        if sealed.expired(datetime.now(UTC)):
            msg = (
                f"{name}: the connection in {self._param!r} expired at "
                f"{sealed.expires_at.isoformat()}; {self.RESEAL_HINT}"
            )
            raise RefusalError(ConnectionRefusal.SEAL_EXPIRED, msg)

        return self._profile(name, sealed)

    def _opened(self, tool_name: str, value: object) -> SealedConnection:
        if not isinstance(value, str):
            msg = (
                f"{tool_name} needs a sealed connection in {self._param!r}, got "
                f"a value of type {type(value).__name__}; {self.RESEAL_HINT}"
            )
            raise RefusalError(ConnectionRefusal.NOT_SEALED, msg)

        try:
            return self._keys.open(value)
        except RefusalError as exc:
            msg = f"{tool_name}: {self._param!r}: {exc}; {self.RESEAL_HINT}"
            raise RefusalError(exc.kind, msg) from exc

    def _profile(self, tool_name: str, sealed: SealedConnection) -> ConnectionBase:
        try:
            connection = self._types_ref().parse(sealed.profile)
        except ConnectionTypesError as exc:
            msg = (
                f"{tool_name}: the sealed connection in {self._param!r} is not a "
                f"connection profile: {exc}"
            )
            raise RefusalError(ConnectionRefusal.SEAL_DAMAGED, msg) from exc

        if connection.kind != self.kind:
            msg = (
                f"{tool_name}: {self._param!r} expects a connection of kind "
                f"{self.kind!r}, got a connection of kind {connection.kind!r}; "
                f"pass a connection of kind {self.kind!r}"
            )
            raise RefusalError(ConnectionRefusal.ANOTHER_KIND, msg)

        return connection


class SealedConnectionParams(NodeArgs, ParamSource):
    """Постановка SealedConnectionParam на параметры-соединения инструментов
    и предложение соединений вызывающему.

    Создаёт его провайдер процесса (connection_params) из ключевой пары
    исполнителя, реестра типов соединений, заданных соединений сервера
    (servers; у чата и стендов их нет) и довооружения кредами (tickets); сама
    постановка и схема параметра — у ConnectionParamHooks. Загрузчик
    инструментов называет списки заданных соединений у инструментов
    (listed) до сборки инструментов. Помнит обвязки по инструментам и этим
    реализует NodeArgs: исполнитель до старта DAG проверяет значения каждого
    узла. offered() строит копии инструментов под предложение вызывающему:
    схема параметра по таблице предложения, проверка значения на вызове.
    """

    def __init__(
        self,
        keys: SealKeys,
        types_ref: TypesRef,
        contexts: CallContexts,
        servers: Mapping[str, ServerConnection],
        tickets: ConfigArming,
    ) -> None:
        self._keys = keys
        self._types_ref = types_ref
        self._contexts = contexts
        self._tickets = tickets
        self._servers = dict(servers)
        self._hooks = ConnectionParamHooks(types_ref)
        self._schemas = ToolSchema()
        self._params: dict[str, dict[str, SealedConnectionParam]] = {}
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
    ) -> list[HostedTool]:
        """Инструменты под предложение вызывающему с ролями roles и
        источниками sources: инструмент без параметров-соединений — как
        есть; с ними — копия, у которой схема параметра по предложению и
        значение на вызове проверяется по нему же. Параметру, которому
        предложить нечего, инструмента в списке нет."""
        offered: list[HostedTool] = []
        for tool in tools:
            params = self._params.get(tool.name)
            if not params:
                offered.append(tool)
                continue

            fields: dict[str, tuple[Any, FieldInfo]] = {}
            for param, hook in params.items():
                names: dict[str, str] = {}
                if OfferedSource.SERVER in sources:
                    names = self._visible(tool.name, roles)

                sealed = OfferedSource.SEALED in sources
                offer = ConnectionOffer(kind=hook.kind, sealed=sealed, names=names)
                if not offer.sealed and not offer.names:
                    break

                checked = AfterValidator(partial(self._checked, offer))
                fields[param] = self._hooks.field(offer, checked)
            else:
                copy = tool.copy()
                copy.args_schema = self._schemas.rebuild(tool.args_schema, fields, ())
                offered.append(copy)

        return offered

    def features(self) -> Mapping[str, Mapping[str, object]]:
        """Возможности сервера для объявления клиентам: приём запечатанных
        соединений с текущим ключом, если хоть один инструмент их берёт."""
        if not self._params:
            return {}

        return {SealFeature.ID: self._keys.feature().model_dump()}

    async def check(self, tool: str, args: Mapping[str, object]) -> None:
        for param, hook in self._params.get(tool, {}).items():
            hook.verified(tool, args.get(param))

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
        """Значение параметра по предложению offer строго по порядку: имя
        предложенного соединения сервера, затем запечатанное значение, если
        оно предложено; иначе отказ с предложенными именами."""
        if value in offer.names:
            return value

        sealed = self._keys.sealed(value)
        if offer.sealed and sealed:
            return value

        names = ", ".join(offer.names)
        if sealed:
            msg = (
                "sealed connections are not offered to this caller, use one "
                f"of: {names}"
            )
            raise ValueError(msg)

        if offer.sealed:
            msg = (
                f"unknown connection {value!r}: expected a sealed connection "
                f"or one of: {names}"
            )
            raise ValueError(msg)

        msg = f"unknown connection {value!r}: expected one of {names}"
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
        hook = SealedConnectionParam(
            self._keys,
            self._types_ref,
            self._contexts,
            self._tickets,
            whitelist,
            param,
            kind,
        )
        self._params.setdefault(tool, {})[param] = hook

        return hook
