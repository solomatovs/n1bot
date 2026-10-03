"""Запечатывание соединений на стороне клиента сервера инструментов.

Модель ставит на место параметра-соединения ссылку из каталога
(ConnectionRef). Клиент перед отправкой вызова находит такие места по
метке схемы инструмента, берёт соединение пользователя, запечатывает его
открытым ключом сервера и подставляет вместо ссылки. Ключ сервер объявляет
среди своих возможностей при подключении клиента (SealFeature); модель про
ключ не знает. Здесь три части:

- ConnectionParams — параметры-соединения инструмента по его схеме.
- SentConnections — что ушло серверу вместо ссылок в идущих вызовах: по нему
  клиент показывает пользователю ссылку, а не запечатанное значение.
- SealingToolServer — порт ToolServer поверх другого порта: запечатывает
  ссылки в аргументах и передаёт вызов дальше.

Отказ запечатывания (сервер не принимает соединения, ссылка не выдана
пользователю, не тот вид соединения, нет делегированных кредов) наружу не
выходит: он возвращается итогом-ошибкой вызова с kind из ConnectionRefusal.

Ошибки:
TypeError — схема вызова инструмента не модель pydantic v2 и не JSON-схема.
ConnectionStoreError — таблица соединений недоступна.
KerberosError — билет к соединению не выпущен.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any, ClassVar

from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ValidationError

from boba.connection_broker.user_connections import ArmedConnections
from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import (
    ConnectionRef,
    ConnectionRefs,
    ConnectionSeal,
    SealFeature,
    SealKey,
)
from boba.identity.errors import RefusalError
from boba.toolkit.failure import ToolRefusalError, ValidationText
from boba.toolrun.stream_calls import CallDag, CallReply, ToolServer, WorkflowTool

__all__ = ["ConnectionParams", "SealingToolServer", "SentConnections"]

logger = logging.getLogger(__name__)


class WorkflowNode:
    """Ключи узла вызова-связки: формат описания DAG (boba.toolkit.dag)."""

    NODES: ClassVar[str] = CallDag.NODES
    TOOL: ClassVar[str] = "tool"
    ARGS: ClassVar[str] = "args"


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

    def __call__(self, ended: asyncio.Future[ToolMessage]) -> None:
        self._sent.forget(self._args)


class ConnectionParams:
    """Параметры-соединения инструмента: имя параметра → вид соединения.

    Параметр помечен в схеме инструмента ключом ConnectionRef.SCHEMA_MARK;
    метку ставит сервер на параметрах верхнего уровня. Создаётся на один
    инструмент из его схемы.
    """

    def __init__(self, schema: Mapping[str, object]) -> None:
        self._kinds: dict[str, str] = {}

        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            return

        for name, declared in properties.items():
            if not isinstance(declared, Mapping):
                continue

            kind = declared.get(ConnectionRef.SCHEMA_MARK)
            if isinstance(kind, str):
                self._kinds[name] = kind

    def kinds(self) -> Mapping[str, str]:
        return self._kinds


class SealingToolServer(ToolServer):
    """Порт ToolServer, запечатывающий соединения перед отправкой вызова.

    Стоит на стороне клиента поверх порта сервера инструментов. Ссылку
    модели на соединение заменяет профилем пользователя, запечатанным ключом
    сервера; остальное передаёт как есть. Ключ берёт из возможностей,
    которые сервер объявил при подключении (SealFeature), на каждый вызов —
    свежесть ключа держит порт сервера. Отказ запечатывания — итог-ошибка
    вызова с подсказкой модели. В истории клиента остаётся исходный вызов со
    ссылкой: аргументы меняются в копии.
    """

    def __init__(
        self,
        inner: ToolServer,
        connections: ArmedConnections,
        sent: SentConnections,
        ttl: timedelta,
    ) -> None:
        self._inner = inner
        self._connections = connections
        self._sent = sent
        self._ttl = ttl
        self._refs = ConnectionRefs()
        self._params: dict[str, ConnectionParams] = {}
        for tool in inner.tools():
            self._params[tool.name] = self._params_of(tool)

    def tools(self) -> Sequence[BaseTool]:
        return self._inner.tools()

    def features(self) -> Mapping[str, Mapping[str, object]]:
        return self._inner.features()

    async def submit(
        self, calls: Sequence[ToolCall]
    ) -> Sequence[asyncio.Future[ToolMessage]]:
        """Запечатанные вызовы уходят серверу одним пакетом; вызов с отказом
        запечатывания серверу не уходит и получает готовый отказ."""
        pending: dict[int, asyncio.Future[ToolMessage]] = {}
        sent: list[ToolCall] = []
        positions: list[int] = []
        for position, call in enumerate(calls):
            try:
                sent.append(await self._sealed(call))
            except ToolRefusalError as exc:
                logger.warning(
                    "sealing connections of %s refused: %s", call["name"], exc
                )
                pending[position] = CallReply(call).refused(exc.failure())
                continue

            positions.append(position)

        accepted = await self._inner.submit(sent)
        for position, call, future in zip(positions, sent, accepted, strict=True):
            future.add_done_callback(SentForget(self._sent, call["args"]))
            pending[position] = future

        ordered: list[asyncio.Future[ToolMessage]] = []
        for position in range(len(calls)):
            ordered.append(pending[position])

        return ordered

    async def _sealed(self, call: ToolCall) -> ToolCall:
        args = await self._sealed_args(call["name"], call["args"])

        return ToolCall(name=call["name"], args=args, id=call["id"], type="tool_call")

    async def _sealed_args(
        self, tool: str, args: Mapping[str, object]
    ) -> dict[str, Any]:
        """Аргументы вызова tool с запечатанными соединениями.

        У инструмента-связки (возможность WorkflowTool.FEATURE) соединения
        лежат в аргументах узлов: каждый узел — вызов своего инструмента.
        """
        if tool == self._workflow_tool():
            return await self._sealed_nodes(args)

        sent = dict(args)

        params = self._params.get(tool)
        if params is None:
            return sent

        for param, kind in params.kinds().items():
            value = sent.get(param)
            if isinstance(value, str):
                sent[param] = await self._seal(value, kind)

        return sent

    async def _sealed_nodes(self, args: Mapping[str, object]) -> dict[str, Any]:
        nodes = args.get(WorkflowNode.NODES)
        if not isinstance(nodes, list):
            return dict(args)

        sealed: list[object] = []
        for node in nodes:
            sealed.append(await self._sealed_node(node))

        sent = dict(args)
        sent[WorkflowNode.NODES] = sealed

        return sent

    async def _sealed_node(self, node: object) -> object:
        """Узел связки с запечатанными аргументами; негодный узел уходит
        серверу как есть — он и ответит, что с ним не так."""
        if not isinstance(node, Mapping):
            return node

        tool = node.get(WorkflowNode.TOOL)
        args = node.get(WorkflowNode.ARGS)
        if not isinstance(tool, str):
            return node

        if not isinstance(args, Mapping):
            return node

        sent = dict(node)
        sent[WorkflowNode.ARGS] = await self._sealed_args(tool, args)

        return sent

    def _workflow_tool(self) -> str | None:
        """Имя инструмента-связки сервера; None — сервер такой не объявил."""
        declared = self._inner.features().get(WorkflowTool.FEATURE)
        if declared is None:
            return None

        name = declared.get("tool")
        if isinstance(name, str):
            return name

        return None

    async def _seal(self, value: str, kind: str) -> str:
        """Ссылка → запечатанный профиль; не ссылка уходит серверу как есть."""
        if not self._refs.is_ref(value):
            return value

        ref = self._refs.parse(value)
        if ref.kind != kind:
            msg = (
                f"connection {value!r} is of kind {ref.kind!r}, the parameter "
                f"expects a connection of kind {kind!r}; pick a connection of "
                f"kind {kind!r} from connection_list"
            )
            raise RefusalError(ConnectionRefusal.ANOTHER_KIND, msg)

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

    @staticmethod
    def _params_of(tool: BaseTool) -> ConnectionParams:
        schema = tool.tool_call_schema
        if isinstance(schema, dict):
            return ConnectionParams(schema)

        if issubclass(schema, BaseModel):
            return ConnectionParams(schema.model_json_schema())

        msg = (
            f"tool {tool.name!r}: expected a pydantic v2 model or a JSON schema "
            f"as its call schema, got {schema.__name__}"
        )
        raise TypeError(msg)
