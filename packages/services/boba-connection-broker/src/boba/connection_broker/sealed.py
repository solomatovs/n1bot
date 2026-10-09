"""Соединения инструментов на стороне исполнителя: приём запечатанных значений.

Исполнитель соединений не хранит. Клиент присылает профиль на месте
параметра-соединения запечатанным открытым ключом исполнителя
(boba.connections.sealed); ключ клиент узнаёт из возможностей сервера при
подключении. SealedConnectionParams ставит на параметры-соединения обвязку
SealedConnectionParam: она открывает значение, сверяет его с вызовом и отдаёт
телу готовый профиль. Он же — правило аргументов узла (NodeArgs): исполнитель
проверяет печати всех узлов до старта DAG и не возвращает запечатанные
значения в итоге вызова; и он же объявляет возможность сервера с ключом.

Ошибки:
RefusalError — значение не запечатано, запечатано другим ключом, повреждено,
    просрочено, выдано другому пользователю либо несёт соединение другого
    вида; kind из ConnectionRefusal.
ToolConfigError — параметр объявлен непригодной моделью соединения.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import ClassVar

from boba.connection_broker.user_connections import (
    ConnectionParamHooks,
    TypesRef,
)
from boba.connections.base import ClientIdentity, ConnectionBase
from boba.connections.manifest import ConnectionTypesError
from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import SealedConnection, SealFeature, SealKeys
from boba.identity.context import CallContexts
from boba.identity.errors import RefusalError
from boba.toolrun.hosted import HostedTool
from boba.toolrun.injected import AsyncInjected, ParamSource
from boba.toolrun.stream_calls import NodeArgs

__all__ = ["SealedConnectionParam", "SealedConnectionParams"]

logger = logging.getLogger(__name__)


class SealedConnectionParam(AsyncInjected):
    """Обвязка одного параметра-соединения на стороне исполнителя.

    Клиент прислал на месте параметра запечатанный профиль; обвязка
    открывает его ключом исполнителя, сверяет с вызовом (пользователь, срок,
    вид соединения), собирает профиль по реестру типов и подписывает его
    клиентом вызова. Тело получает готовый профиль. Ставит её
    SealedConnectionParams.
    """

    ARGUMENT: ClassVar[str] = (
        "Соединение пользователя вида {kind}.\n"
        "   - профиль соединения, запечатанный открытым ключом сервера: "
        "компактная JWE, содержимое по contentSchema\n"
        "   - соединение другого вида сервер не принимает"
    )
    """Описание параметра-соединения в схеме инструмента; {kind} — вид."""

    APPLICATION: ClassVar[str] = "boba"
    """Имя приложения в подписи профиля: под ним ходят все инструменты."""

    RESEAL_HINT: ClassVar[str] = (
        "repeat the call with the connection name instead of the sealed value"
    )

    def __init__(
        self,
        keys: SealKeys,
        types_ref: TypesRef,
        contexts: CallContexts,
        param: str,
        kind: str,
    ) -> None:
        super().__init__(param, None)
        self._keys = keys
        self._contexts = contexts
        self._types_ref = types_ref
        self._kind = kind

    async def value(self, name: str, kwargs: dict[str, object]) -> object:
        connection = self.verified(name, kwargs.get(self._param))
        login = self._contexts.subject().login
        client = ClientIdentity(application=self.APPLICATION, login=login, tool=name)
        labelled = connection.labeled(client)

        logger.info(
            "tool %s: sealed connection in %s (%s) %s",
            name,
            self._param,
            self._kind,
            labelled.trace(),
        )

        return labelled

    def shown(self, value: object) -> str:
        """Чем параметр показывается в итоге вызова вместо запечатанного
        значения value: именем, которым соединение назвал вызывающий.
        Значение, которое не открывается (не запечатано, повреждено),
        имени не несёт и показывается пометкой вида соединения."""
        if not isinstance(value, str):
            return f"<sealed {self._kind} connection>"

        try:
            return self._keys.open(value).name
        except RefusalError:
            return f"<sealed {self._kind} connection>"

    def verified(self, name: str, value: object) -> ConnectionBase:
        """Профиль из запечатанного значения, сверенный с вызовом."""
        sealed = self._opened(name, value)
        login = self._contexts.subject().login

        if sealed.login != login:
            msg = (
                f"{name}: the connection in {self._param!r} is sealed for user "
                f"{sealed.login!r}, the call is made by {login!r}"
            )
            raise RefusalError(ConnectionRefusal.SEALED_FOR_ANOTHER_USER, msg)

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

        if connection.kind != self._kind:
            msg = (
                f"{tool_name}: {self._param!r} expects a connection of kind "
                f"{self._kind!r}, got a connection of kind {connection.kind!r}; "
                f"pass a connection of kind {self._kind!r}"
            )
            raise RefusalError(ConnectionRefusal.ANOTHER_KIND, msg)

        return connection


class SealedConnectionParams(NodeArgs, ParamSource):
    """Постановка SealedConnectionParam на параметры-соединения инструментов.

    Создаёт её сборка исполнителя из его ключевой пары и реестра типов
    соединений; сама постановка и правка схемы для LLM — у
    ConnectionParamHooks. Помнит обвязки по инструментам и этим реализует
    NodeArgs: исполнитель до старта DAG проверяет печати каждого узла, а в
    итоге вызова показывает параметры-соединения без значений.
    """

    def __init__(
        self, keys: SealKeys, types_ref: TypesRef, contexts: CallContexts
    ) -> None:
        self._keys = keys
        self._types_ref = types_ref
        self._contexts = contexts
        self._hooks = ConnectionParamHooks(types_ref, SealedConnectionParam.ARGUMENT)
        self._params: dict[str, dict[str, SealedConnectionParam]] = {}

    def bind_all(self, tools: Sequence[HostedTool]) -> None:
        self._hooks.bind_all(tools, self._hook)

    def features(self) -> Mapping[str, Mapping[str, object]]:
        """Возможности сервера для объявления клиентам: приём запечатанных
        соединений с текущим ключом, если хоть один инструмент их берёт."""
        if not self._params:
            return {}

        return {SealFeature.ID: self._keys.feature().model_dump()}

    async def check(self, tool: str, args: Mapping[str, object]) -> None:
        for param, hook in self._params.get(tool, {}).items():
            hook.verified(tool, args.get(param))

    def shown(self, tool: str, args: Mapping[str, object]) -> Mapping[str, object]:
        hooks = self._params.get(tool)
        if not hooks:
            return args

        shown = dict(args)
        for param, hook in hooks.items():
            if param in shown:
                shown[param] = hook.shown(shown[param])

        return shown

    def _hook(self, tool: str, param: str, kind: str) -> AsyncInjected:
        hook = SealedConnectionParam(
            self._keys, self._types_ref, self._contexts, param, kind
        )
        self._params.setdefault(tool, {})[param] = hook

        return hook
