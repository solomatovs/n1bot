"""Запечатывание соединений на стороне клиента сервера инструментов.

Модель ставит на место параметра-соединения ссылку из каталога
(ConnectionRef). Клиент перед отправкой вызова находит такие места по
метке схемы инструмента, берёт соединение пользователя, запечатывает его
открытым ключом сервера и подставляет вместо ссылки. Ключ сервер объявляет
среди своих возможностей при подключении клиента (SealFeature); модель про
ключ не знает. Здесь три части:

- MarkedConnections — обход аргументов вызова по схеме инструмента: находит
  значения параметров-соединений на любой глубине.
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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
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
    SealedConnection,
    SealFeature,
    SealKey,
)
from boba.identity.context import CallContext
from boba.identity.errors import RefusalError
from boba.toolkit.failure import ToolRefusalError, ValidationText
from boba.toolkit.types import SecretReveal
from boba.toolrun.stream_calls import CallDag, ToolServer, WorkflowTool

__all__ = ["MarkedConnections", "SealingToolServer", "SentConnections"]

logger = logging.getLogger(__name__)

Replace = Callable[[str, str], Awaitable[str]]
"""Замена значения параметра-соединения: (значение, вид по схеме) → новое."""


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


class MarkedConnections:
    """Обход аргументов вызова по JSON-схеме инструмента.

    Параметр-соединение помечен в схеме ключом ConnectionRef.SCHEMA_MARK со
    значением — видом соединения. Обход идёт по значению и схеме вместе:
    свойства объектов, элементы списков, ветки anyOf/oneOf (узлы workflow
    различаются константами свойств), ссылки $ref. Каждое строковое значение на
    помеченном месте заменяется результатом replace. Создаётся на один
    инструмент из его схемы.
    """

    REF: ClassVar[str] = "$ref"
    DEFS_PREFIX: ClassVar[str] = "#/$defs/"
    BRANCHES: ClassVar[tuple[str, ...]] = ("anyOf", "oneOf")

    def __init__(self, schema: Mapping[str, object]) -> None:
        self._schema = schema
        defs = schema.get("$defs")
        self._defs: Mapping[str, object] = {}
        if isinstance(defs, Mapping):
            self._defs = defs

    async def replaced(self, args: Mapping[str, object], replace: Replace) -> object:
        return await self._walk(args, self._schema, replace)

    async def _walk(
        self, value: object, schema: Mapping[str, object], replace: Replace
    ) -> object:
        schema = self._resolved(schema)

        kind = schema.get(ConnectionRef.SCHEMA_MARK)
        if isinstance(kind, str):
            if isinstance(value, str):
                return await replace(value, kind)

            return value

        for branch in self._branches(schema):
            if self._fits(value, branch):
                value = await self._walk(value, branch, replace)

        if isinstance(value, Mapping):
            return await self._walk_object(value, schema, replace)

        if isinstance(value, list):
            return await self._walk_items(value, schema, replace)

        return value

    async def _walk_object(
        self,
        value: Mapping[str, object],
        schema: Mapping[str, object],
        replace: Replace,
    ) -> object:
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            return value

        walked = dict(value)
        for name, declared in properties.items():
            if name not in walked:
                continue

            if isinstance(declared, Mapping):
                walked[name] = await self._walk(walked[name], declared, replace)

        return walked

    async def _walk_items(
        self, value: Sequence[object], schema: Mapping[str, object], replace: Replace
    ) -> object:
        items = schema.get("items")
        if not isinstance(items, Mapping):
            return value

        walked: list[object] = []
        for item in value:
            walked.append(await self._walk(item, items, replace))

        return walked

    def _branches(self, schema: Mapping[str, object]) -> list[Mapping[str, object]]:
        branches: list[Mapping[str, object]] = []
        for key in self.BRANCHES:
            declared = schema.get(key)
            if not isinstance(declared, list):
                continue

            for branch in declared:
                if isinstance(branch, Mapping):
                    branches.append(self._resolved(branch))

        return branches

    @staticmethod
    def _fits(value: object, branch: Mapping[str, object]) -> bool:
        """Подходит ли ветка значению: константы её свойств совпали."""
        properties = branch.get("properties")
        if not isinstance(properties, Mapping):
            return True

        if not isinstance(value, Mapping):
            return False

        for name, declared in properties.items():
            if not isinstance(declared, Mapping):
                continue

            if "const" not in declared:
                continue

            if value.get(name) != declared["const"]:
                return False

        return True

    def _resolved(self, schema: Mapping[str, object]) -> Mapping[str, object]:
        ref = schema.get(self.REF)
        if not isinstance(ref, str):
            return schema

        if not ref.startswith(self.DEFS_PREFIX):
            return schema

        target = self._defs.get(ref.removeprefix(self.DEFS_PREFIX))
        if not isinstance(target, Mapping):
            return schema

        return target


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
        self._marked: dict[str, MarkedConnections] = {}
        for tool in inner.tools():
            self._marked[tool.name] = self._marked_of(tool)

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
                pending[position] = self._refused(call, exc)
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

        marked = self._marked.get(tool)
        if marked is None:
            return dict(args)

        replaced = await marked.replaced(args, self._seal)
        if not isinstance(replaced, dict):
            return dict(args)

        return replaced

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
        connection = await self._connections.armed(ref)
        sealed = SealedConnection(
            login=CallContext.current().subject.login,
            expires_at=datetime.now(UTC) + self._ttl,
            profile=SecretReveal.dumped(connection),
        )

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
    def _marked_of(tool: BaseTool) -> MarkedConnections:
        schema = tool.tool_call_schema
        if isinstance(schema, dict):
            return MarkedConnections(schema)

        if issubclass(schema, BaseModel):
            return MarkedConnections(schema.model_json_schema())

        msg = (
            f"tool {tool.name!r}: expected a pydantic v2 model or a JSON schema "
            f"as its call schema, got {schema.__name__}"
        )
        raise TypeError(msg)

    @staticmethod
    def _refused(
        call: ToolCall, refusal: ToolRefusalError
    ) -> asyncio.Future[ToolMessage]:
        content, artifact = refusal.failure().packed()
        message = ToolMessage(
            content=content,
            artifact=artifact,
            name=call["name"],
            tool_call_id=call["id"],
            status="error",
        )

        done: asyncio.Future[ToolMessage] = asyncio.get_running_loop().create_future()
        done.set_result(message)

        return done
