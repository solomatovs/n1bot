"""Запечатывание соединений на стороне клиента сервера инструментов.

Какие параметры инструмента — соединения и какого вида, сервер объявляет в
их схеме (SealedParamSchema). Клиент показывает модели на их месте имя
соединения из каталога, а перед отправкой вызова читает ту же схему, берёт
соединение пользователя по виду и имени, запечатывает его открытым ключом
сервера и подставляет вместо имени. Ключ сервер объявляет среди своих
возможностей при подключении клиента (SealFeature); модель про ключ не
знает. Здесь две части:

- SentConnections — что ушло серверу вместо имён в идущих вызовах: по нему
  клиент показывает пользователю имя, а не запечатанное значение.
- SealingToolServer — порт ToolServer поверх другого порта: отдаёт модели
  схему с именами на месте соединений, запечатывает их в аргументах и
  передаёт вызов дальше.

Отказ запечатывания (сервер не принимает соединения, имя не выдано
пользователю, нет делегированных кредов) наружу не выходит: он
возвращается итогом-ошибкой вызова с kind из ConnectionRefusal.

Ошибки:
ConnectionStoreError — таблица соединений недоступна.
KerberosError — билет к соединению не выпущен.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any, ClassVar

from pydantic import JsonValue, ValidationError

from boba.connection_broker.user_connections import ArmedConnections
from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import (
    ConnectionSeal,
    SealedParamSchema,
    SealFeature,
    SealKey,
)
from boba.identity.errors import RefusalError
from boba.toolkit.chain import StreamPlanError
from boba.toolkit.dag import (
    CallDag,
    DagNode,
    NodeCalls,
    NodeOutcome,
    NodeOutcomes,
    ToolCard,
    ToolServer,
)
from boba.toolkit.failure import ToolRefusalError, ValidationText

__all__ = ["SealingToolServer", "SentConnections"]

logger = logging.getLogger(__name__)


class SentConnections:
    """Запечатанные значения идущих вызовов и имена, вместо которых они ушли.

    Пока исполнитель живёт в процессе клиента, лента клиента рисует шаг
    инструмента по аргументам, с которыми тело вызвано, — а там уже
    запечатанное значение. Порт клиента (SealingToolServer) кладёт сюда пары
    на время вызова, лента возвращает имена на место (shown). Один объект
    на процесс клиента: его создаёт сборка и отдаёт обоим — порту и ленте.
    Запечатанное значение неповторимо, поэтому вызовы друг другу не мешают.
    """

    def __init__(self) -> None:
        self._names: dict[str, str] = {}

    def remember(self, sealed: str, name: str) -> None:
        self._names[sealed] = name

    def forget(self, args: object) -> None:
        """Забыть запечатанные значения, лежащие в аргументах вызова args."""
        if isinstance(args, str):
            self._names.pop(args, None)
            return

        if isinstance(args, Mapping):
            for value in args.values():
                self.forget(value)

            return

        if isinstance(args, list):
            for item in args:
                self.forget(item)

    def shown(self, args: Mapping[str, object]) -> Mapping[str, object]:
        """Аргументы вызова для показа: имена на месте запечатанных значений."""
        if not self._names:
            return args

        shown = dict(args)
        for param, value in args.items():
            if not isinstance(value, str):
                continue

            if name := self._names.get(value):
                shown[param] = name

        return shown


class SentForget:
    """Конец вызова: его запечатанные значения клиенту больше не нужны.

    Ставится на ожидание итога вызова; когда оно кончилось — итогом, ошибкой
    или отменой, — пары вызова уходят из SentConnections.
    """

    def __init__(self, sent: SentConnections, args: Mapping[str, object]) -> None:
        self._sent = sent
        self._args = args

    def __call__(self, ended: asyncio.Future[NodeOutcome]) -> None:
        self._sent.forget(self._args)


class RefusedBody:
    """Тело вызова, которому отказано до отправки серверу: отдаёт готовый
    отказ. Создаётся портом SealingToolServer, чтобы отказанный вызов прошёл
    путём вызова узла (NodeCalls) и хозяин клиента с лентой увидел его шагом.
    """

    def __init__(self, refused: NodeOutcome) -> None:
        self._refused = refused

    async def __call__(self, node: DagNode) -> NodeOutcome:
        return self._refused


class SealingToolServer(ToolServer):
    """Порт ToolServer, запечатывающий соединения перед отправкой вызова.

    Стоит на стороне клиента поверх порта сервера инструментов. Параметры
    -соединения и их вид читает из схем инструментов сервера
    (SealedParamSchema): модели отдаёт те же карточки, где на месте такого
    параметра строка-имя соединения, а имя из вызова модели заменяет
    профилем пользователя, запечатанным ключом сервера; остальное передаёт
    как есть. Ключ берёт из возможностей, которые сервер объявил при
    подключении (SealFeature), на каждый вызов — свежесть ключа держит порт
    сервера. Отказ запечатывания — итог-ошибка вызова с подсказкой модели;
    такой вызов идёт путём вызова узла (conduct), как и отправленный серверу.
    В истории клиента остаётся исходный вызов с именем: аргументы меняются в
    копии.
    """

    OFFERED: ClassVar[str] = (
        "Имя соединения пользователя вида {kind}: колонка name выдачи "
        "connection_list или connection_search, как есть."
    )
    """Описание параметра-соединения для модели; {kind} — вид соединения."""

    STRING_KEYS: ClassVar[tuple[str, ...]] = ("title", "minLength", "type")
    """Ключи схемы сервера, которые остаются у строки-имени для модели."""

    def __init__(
        self,
        inner: ToolServer,
        connections: ArmedConnections,
        sent: SentConnections,
        ttl: timedelta,
        conduct: NodeCalls,
    ) -> None:
        self._inner = inner
        self._conduct = conduct
        self._connections = connections
        self._sent = sent
        self._ttl = ttl
        self._declared = SealedParamSchema()
        self._outcomes = NodeOutcomes()
        self._dags = CallDag()

    def tools(self) -> Sequence[ToolCard]:
        """Карточки сервера для модели: параметр-соединение — строка-имя."""
        offered: list[ToolCard] = []
        for card in self._inner.tools():
            offered.append(self._offered(card))

        return offered

    def _offered(self, card: ToolCard) -> ToolCard:
        kinds = self._declared.kinds_of(card.parameters)
        if not kinds:
            return card

        parameters = dict(card.parameters)
        properties = dict(parameters[SealedParamSchema.PROPERTIES])
        for param, kind in kinds.items():
            properties[param] = self._named(properties[param], kind)

        parameters[SealedParamSchema.PROPERTIES] = properties

        return ToolCard(
            name=card.name,
            description=card.description,
            parameters=parameters,
            views=card.views,
        )

    def _named(self, declared: Mapping[str, Any], kind: str) -> dict[str, Any]:
        """Схема параметра для модели: строка с именем соединения вида kind."""
        named: dict[str, Any] = {}
        for key in self.STRING_KEYS:
            if key in declared:
                named[key] = declared[key]

        named["description"] = self.OFFERED.format(kind=kind)

        return named

    def features(self) -> Mapping[str, Mapping[str, object]]:
        return self._inner.features()

    async def submit(
        self, calls: Sequence[DagNode]
    ) -> Sequence[asyncio.Future[NodeOutcome]]:
        """Запечатанные вызовы уходят серверу одним пакетом; вызов с отказом
        запечатывания серверу не уходит и получает готовый отказ."""
        pending: dict[int, asyncio.Future[NodeOutcome]] = {}
        sent: list[DagNode] = []
        positions: list[int] = []
        for position, call in enumerate(calls):
            try:
                sent.append(await self._sealed(call))
            except ToolRefusalError as exc:
                logger.warning("sealing connections of %s refused: %s", call.tool, exc)
                refused = self._outcomes.refused(call, exc.failure())
                pending[position] = self._shown(call, refused)
                continue

            positions.append(position)

        accepted = await self._inner.submit(sent)
        for position, call, future in zip(positions, sent, accepted, strict=True):
            future.add_done_callback(SentForget(self._sent, call.args))
            pending[position] = future

        ordered: list[asyncio.Future[NodeOutcome]] = []
        for position in range(len(calls)):
            ordered.append(pending[position])

        return ordered

    def _shown(
        self, call: DagNode, refused: NodeOutcome
    ) -> asyncio.Future[NodeOutcome]:
        """Отказ вызову call путём вызова узла: хозяин клиента узнаёт о нём,
        как о любом вызове. Инструмента с таким именем у сервера нет — пути
        вызова нет, отказ отдаётся как есть."""
        card = self._card_of(call.tool)
        if card is None:
            return self._outcomes.settled(refused)

        body = RefusedBody(refused)

        return asyncio.ensure_future(self._conduct.conducted(card, call, body))

    def _card_of(self, tool: str) -> ToolCard | None:
        """Карточка инструмента сервера, как он её объявил; None — нет такого."""
        for card in self._inner.tools():
            if card.name == tool:
                return card

        return None

    async def _sealed(self, call: DagNode) -> DagNode:
        """Вызов с запечатанными соединениями.

        У инструмента-связки (CallDag.WORKFLOW) соединения лежат в
        аргументах узлов: каждый узел — вызов своего инструмента. Связка,
        которая не проходит форму описания, уходит серверу как есть — он и
        ответит, что с ней не так.
        """
        if call.tool != self._dags.WORKFLOW:
            return await self._sealed_call(call)

        try:
            described = self._dags.described(call)
        except StreamPlanError:
            return call

        nodes: list[DagNode] = []
        for node in described:
            nodes.append(await self._sealed_call(node))

        return self._dags.with_nodes(call, nodes)

    async def _sealed_call(self, call: DagNode) -> DagNode:
        """Вызов одного инструмента с запечатанными соединениями: каждый
        параметр, который схема инструмента сервера объявляет соединением,
        получает запечатанный профиль по имени из вызова модели. Значение не
        строка — уходит как есть, его отвергнет сервер."""
        card = self._card_of(call.tool)
        if card is None:
            return call

        sent: dict[str, JsonValue] = dict(call.args)
        for param, kind in self._declared.kinds_of(card.parameters).items():
            value = call.args.get(param)
            if not isinstance(value, str):
                continue

            sent[param] = await self._seal(kind, value)

        return call.model_copy(update={"args": sent})

    async def _seal(self, kind: str, name: str) -> str:
        """Имя соединения вида kind → запечатанный профиль."""
        key = self._key(kind, name)
        sealed = await self._connections.sealed(kind, name, self._ttl)

        sent = ConnectionSeal(key).seal(sealed)
        self._sent.remember(sent, name)

        return sent

    def _key(self, kind: str, name: str) -> SealKey:
        """Ключ сервера из объявленной им возможности."""
        declared = self._inner.features().get(SealFeature.ID)
        if declared is None:
            msg = (
                f"connection {name!r} of kind {kind!r} cannot be sent: the tool "
                f"server does not declare the feature {SealFeature.ID!r}, so it "
                "does not accept connections"
            )
            raise RefusalError(ConnectionRefusal.SEAL_KEY_UNKNOWN, msg)

        try:
            return SealFeature.model_validate(declared).key
        except ValidationError as exc:
            msg = (
                f"connection {name!r} of kind {kind!r} cannot be sent: the feature "
                f"{SealFeature.ID!r} of the tool server does not carry an "
                f"encryption key: {ValidationText.of(exc)}"
            )
            raise RefusalError(ConnectionRefusal.SEAL_KEY_UNKNOWN, msg) from exc
