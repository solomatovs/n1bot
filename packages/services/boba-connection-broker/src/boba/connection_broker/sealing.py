"""Запечатывание соединений на стороне клиента сервера инструментов.

Модель ставит на место параметра-соединения ссылку из каталога
(ConnectionRef). Клиент перед отправкой вызова находит такие значения
среди аргументов, берёт соединение пользователя, запечатывает его открытым
ключом сервера и подставляет вместо ссылки. Ключ сервер объявляет среди
своих возможностей при подключении клиента (SealFeature); модель про ключ
не знает. Здесь две части:

- SentConnections — что ушло серверу вместо ссылок в идущих вызовах: по нему
  клиент показывает пользователю ссылку, а не запечатанное значение.
- SealingToolServer — порт ToolServer поверх другого порта: запечатывает
  ссылки в аргументах и передаёт вызов дальше.

Отказ запечатывания (сервер не принимает соединения, ссылка не выдана
пользователю, не тот вид соединения, нет делегированных кредов) наружу не
выходит: он возвращается итогом-ошибкой вызова с kind из ConnectionRefusal.

Ошибки:
ConnectionStoreError — таблица соединений недоступна.
KerberosError — билет к соединению не выпущен.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import timedelta

from pydantic import JsonValue, ValidationError

from boba.connection_broker.user_connections import ArmedConnections
from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import (
    ConnectionRefs,
    ConnectionSeal,
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
    """Запечатанные значения идущих вызовов и ссылки, вместо которых они ушли.

    Пока исполнитель живёт в процессе клиента, лента клиента рисует шаг
    инструмента по аргументам, с которыми тело вызвано, — а там уже
    запечатанное значение. Порт клиента (SealingToolServer) кладёт сюда пары
    на время вызова, лента возвращает ссылки на место (shown). Один объект
    на процесс клиента: его создаёт сборка и отдаёт обоим — порту и ленте.
    Запечатанное значение неповторимо, поэтому вызовы друг другу не мешают.
    """

    def __init__(self) -> None:
        self._refs: dict[str, str] = {}

    def remember(self, sealed: str, ref: str) -> None:
        self._refs[sealed] = ref

    def forget(self, args: object) -> None:
        """Забыть запечатанные значения, лежащие в аргументах вызова args."""
        if isinstance(args, str):
            self._refs.pop(args, None)
            return

        if isinstance(args, Mapping):
            for value in args.values():
                self.forget(value)

            return

        if isinstance(args, list):
            for item in args:
                self.forget(item)

    def shown(self, args: Mapping[str, object]) -> Mapping[str, object]:
        """Аргументы вызова для показа: ссылки на месте запечатанных значений."""
        if not self._refs:
            return args

        shown = dict(args)
        for name, value in args.items():
            if not isinstance(value, str):
                continue

            if ref := self._refs.get(value):
                shown[name] = ref

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

    Стоит на стороне клиента поверх порта сервера инструментов. Ссылку
    модели на соединение заменяет профилем пользователя, запечатанным ключом
    сервера; остальное передаёт как есть. Ключ берёт из возможностей,
    которые сервер объявил при подключении (SealFeature), на каждый вызов —
    свежесть ключа держит порт сервера. Отказ запечатывания — итог-ошибка
    вызова с подсказкой модели; такой вызов идёт путём вызова узла (conduct),
    как и отправленный серверу. В истории клиента остаётся исходный вызов со
    ссылкой: аргументы меняются в копии.
    """

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
        self._refs = ConnectionRefs()
        self._outcomes = NodeOutcomes()
        self._dags = CallDag()
        self._cards: dict[str, ToolCard] = {}
        for card in inner.tools():
            self._cards[card.name] = card

    def tools(self) -> Sequence[ToolCard]:
        return self._inner.tools()

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
        card = self._cards.get(call.tool)
        if card is None:
            return self._outcomes.settled(refused)

        body = RefusedBody(refused)

        return asyncio.ensure_future(self._conduct.conducted(card, call, body))

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
        аргумент верхнего уровня, чьё значение — ссылка на соединение
        (ConnectionRef), заменяется запечатанным профилем."""
        if call.tool not in self._cards:
            return call

        sent: dict[str, JsonValue] = dict(call.args)
        for param, value in call.args.items():
            if not isinstance(value, str):
                continue

            if not self._refs.is_ref(value):
                continue

            sent[param] = await self._seal(value)

        return call.model_copy(update={"args": sent})

    async def _seal(self, value: str) -> str:
        """Ссылка на соединение → запечатанный профиль. Подходит ли вид
        соединения параметру, решает сервер: он открывает профиль и сверяет
        вид с тем, что ждёт инструмент."""
        ref = self._refs.parse(value)
        key = self._key(value)
        sealed = await self._connections.sealed(ref, self._ttl)

        sent = ConnectionSeal(key).seal(sealed)
        self._sent.remember(sent, value)

        return sent

    def _key(self, value: str) -> SealKey:
        """Ключ сервера из объявленной им возможности."""
        declared = self._inner.features().get(SealFeature.ID)
        if declared is None:
            msg = (
                f"connection {value!r} cannot be sent: the tool server does "
                f"not declare the feature {SealFeature.ID!r}, so it does not "
                "accept connections"
            )
            raise RefusalError(ConnectionRefusal.SEAL_KEY_UNKNOWN, msg)

        try:
            return SealFeature.model_validate(declared).key
        except ValidationError as exc:
            msg = (
                f"connection {value!r} cannot be sent: the feature "
                f"{SealFeature.ID!r} of the tool server does not carry an "
                f"encryption key: {ValidationText.of(exc)}"
            )
            raise RefusalError(ConnectionRefusal.SEAL_KEY_UNKNOWN, msg) from exc
