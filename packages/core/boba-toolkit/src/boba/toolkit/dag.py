"""Описание DAG вызовов инструментов и его перевод в план исполнения.

DagSpec — сериализуемая модель графа: узлы — вызовы инструментов в том
виде, в каком их делает модель (имя и аргументы), каналы названы в
аргументах полями с именами портов инструмента. Описание живёт отдельно от
исполнения: его даёт ответ модели, файл или хранилище, а исполнитель
принимает только его. DagPlanner переводит описание в планы групп
исполнения (StreamPlan) по декларациям портов инструментов: узлы, связанные
каналами, собираются в одну группу и исполняются по правилу «все или никто»,
узел без каналов — группа из него одного. Все проверки графа каналов
остаются в StreamPlan. WorkflowResult — итог вызова связки для клиента:
результаты узлов одним конвертом.

Здесь же контракт порта сервера инструментов — типы, которыми клиент порта
(чат, сервис MCP, клиент MCP) и его исполнитель говорят друг с другом:
карточка инструмента ToolCard, итог вызова NodeOutcome с фабрикой
NodeOutcomes и адресом журнала JournalAddress, путь вызова узла к телу
NodeCalls, сам порт ToolServer; CallDag строит описание DAG по вызовам
клиента порта и называет инструмент-связку. Реализации порта живут в
boba.toolrun и клиенте MCP.

Ошибки:
StreamPlanError — поле порта узла не имя канала, pipe_bytes не число либо
    граф нарушен (правила StreamPlan); вызов workflow не проходит форму
    описания DAG; настройки возможности связки не проходят её модель.
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    model_validator,
)

from boba.toolkit.calls import FieldView
from boba.toolkit.chain import (
    StreamInput,
    StreamNode,
    StreamOutput,
    StreamPlan,
    StreamPlanError,
)
from boba.toolkit.failure import ValidationText
from boba.toolkit.ports import PortDecl, StreamSpec
from boba.toolkit.result import (
    ChatView,
    Fact,
    FactsBlock,
    FailureResult,
    ToolResult,
    ToolResultBase,
)

__all__ = [
    "CallDag",
    "DagNode",
    "DagPlanner",
    "DagSpec",
    "JournalAddress",
    "NodeBody",
    "NodeCalls",
    "NodeOutcome",
    "NodeOutcomes",
    "NodeReports",
    "ToolCard",
    "ToolHints",
    "ToolServer",
    "WorkflowNodeResult",
    "WorkflowResult",
]


class DagNode(BaseModel):
    """Узел DAG: вызов инструмента — его имя и аргументы вызова.

    Поля каналов (имена портов инструмента) и pipe_bytes лежат среди
    аргументов, как в вызове модели; ключ узла в DAG уникален, из чата им
    служит tool_call_id. title — имя узла, каким его назвал автор описания
    (ключ узла вызова workflow): им узел называется в текстах отказов.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    args: Mapping[str, JsonValue] = {}
    title: str = ""
    call_id: str = ""
    """Идентификатор вызова узла, заданный клиентом, который сам собрал
    вызов workflow из отдельных вызовов модели: под ним идут журнал и шаг
    узла. Пусто — исполнитель выводит его из идентификатора вызова workflow."""

    def label(self) -> str:
        if self.title:
            return f"{self.tool} ({self.title})"

        return f"{self.tool} ({self.key})"


class DagSpec(BaseModel):
    """Описание DAG: имя, версия и узлы; ключи узлов уникальны."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    nodes: Sequence[DagNode] = Field(min_length=1)

    @model_validator(mode="after")
    def _keys_are_unique(self) -> DagSpec:
        repeated = sorted(set(self._repeated_keys()))
        if repeated:
            msg = (
                f"dag {self.name!r} v{self.version}: node keys must be unique, "
                f"repeated: {repeated}"
            )
            raise ValueError(msg)

        return self

    def _repeated_keys(self) -> Iterator[str]:
        seen: set[str] = set()
        for node in self.nodes:
            if node.key in seen:
                yield node.key

            seen.add(node.key)

    def node(self, key: str) -> DagNode:
        for node in self.nodes:
            if node.key == key:
                return node

        msg = f"dag {self.name!r} v{self.version}: no node with key {key!r}"
        raise KeyError(msg)


@dataclass
class _Component:
    """Набор узлов, уже связанных каналами, и имена этих каналов."""

    nodes: list[StreamNode]
    channels: set[str]


class ChannelComponents:
    """Связные компоненты узлов по общим именам каналов.

    DagPlanner отдаёт сюда узлы в порядке описания; узел, называющий канал
    уже собранной компоненты, присоединяется к ней, а называющий каналы
    нескольких — сливает их в одну. Узел без общих каналов остаётся
    компонентой из себя одного.
    """

    def __init__(self) -> None:
        self._components: list[_Component] = []
        self._order: dict[str, int] = {}

    def add(self, node: StreamNode) -> None:
        self._order[node.key] = len(self._order)
        channels = set(self._channels_of(node))

        joined = _Component(nodes=[node], channels=set(channels))
        apart: list[_Component] = []
        for component in self._components:
            if component.channels.isdisjoint(channels):
                apart.append(component)
                continue

            joined.nodes.extend(component.nodes)
            joined.channels.update(component.channels)

        apart.append(joined)
        self._components = apart

    def components(self) -> Iterator[_Component]:
        """Компоненты в порядке их первых узлов, узлы — в порядке описания."""
        ordered: list[_Component] = []
        for component in self._components:
            nodes = sorted(component.nodes, key=self._position)
            ordered.append(_Component(nodes=nodes, channels=component.channels))

        ordered.sort(key=self._first_position)

        yield from ordered

    def _position(self, node: StreamNode) -> int:
        return self._order[node.key]

    def _first_position(self, component: _Component) -> int:
        return self._order[component.nodes[0].key]

    @staticmethod
    def _channels_of(node: StreamNode) -> Iterator[str]:
        for bound in node.inputs:
            yield bound.channel

        for output in node.outputs:
            yield output.channel


class DagPlanner:
    """Перевод описания DAG в план исполнения по декларациям инструментов.

    Декларации приходят функцией «имя инструмента → StreamSpec» (в
    приложении — StreamSpecs.of). Для каждого узла планировщик читает
    из аргументов поля портов: у одиночного порта — имя канала строкой, у
    порта-списка — список имён; каждый порт назван обязательно. У писателя
    читается pipe_bytes; узел его не назвал — берётся pipe_bytes
    планировщика (дефолт секции [stream_groups]). Узлы делятся на группы по
    общим каналам: имена каналов и связывают вызовы в группу, узел
    инструмента без портов остаётся группой из себя одного. Правила графа
    каждой группы проверяет StreamPlan.
    """

    PIPE_FIELD: ClassVar[str] = "pipe_bytes"

    def __init__(self, specs: Callable[[str], StreamSpec], pipe_bytes: int) -> None:
        self._specs = specs
        self._default_pipe_bytes = pipe_bytes

    def plan(self, dag: DagSpec) -> tuple[StreamPlan, ...]:
        """Планы групп исполнения в порядке первых узлов описания."""
        nodes: list[StreamNode] = []
        for node in dag.nodes:
            nodes.append(self._node(node, self._specs(node.tool)))

        # опечатка в канале разносит писателя и читателя по группам
        StreamPlan(nodes)

        components = ChannelComponents()
        for planned in nodes:
            components.add(planned)

        plans: list[StreamPlan] = []
        for component in components.components():
            plans.append(StreamPlan(component.nodes))

        return tuple(plans)

    def _node(self, node: DagNode, spec: StreamSpec) -> StreamNode:
        outputs: list[StreamOutput] = []
        for port, channel in self._channels(node, spec.outbound()):
            outputs.append(StreamOutput(port=port, channel=channel))

        inputs: list[StreamInput] = []
        for port, channel in self._channels(node, spec.inbound()):
            inputs.append(StreamInput(port=port, channel=channel))

        pipe_bytes = 0
        if outputs:
            pipe_bytes = self._pipe_bytes(node)

        return StreamNode(
            key=node.key,
            tool=node.tool,
            spec=spec,
            outputs=tuple(outputs),
            inputs=tuple(inputs),
            pipe_bytes=pipe_bytes,
            title=node.title,
        )

    def _channels(
        self, node: DagNode, ports: Sequence[PortDecl]
    ) -> Iterator[tuple[str, str]]:
        """Пары (порт, канал) одной стороны узла из его аргументов."""
        for port in ports:
            value = node.args.get(port.name)
            if value is None:
                msg = (
                    f"stream plan: {node.label()} names no channel for port "
                    f"{port.name!r}; every port of a node is bound explicitly"
                )
                raise StreamPlanError(msg)

            for channel in self._names(node, port, value):
                yield port.name, channel

    def _names(self, node: DagNode, port: PortDecl, value: JsonValue) -> list[str]:
        if not port.many:
            return [self._name(node, port, value)]

        if not isinstance(value, list):
            msg = (
                f"stream plan: {node.label()} field {port.name!r} expects a list "
                f"of channel names, got {value!r}"
            )
            raise StreamPlanError(msg)

        names: list[str] = []
        for item in value:
            names.append(self._name(node, port, item))

        return names

    @staticmethod
    def _name(node: DagNode, port: PortDecl, value: JsonValue) -> str:
        if not isinstance(value, str):
            msg = (
                f"stream plan: {node.label()} field {port.name!r} expects a "
                f"channel name, got {value!r}"
            )
            raise StreamPlanError(msg)

        if not value:
            msg = (
                f"stream plan: {node.label()} field {port.name!r} names an "
                "empty channel"
            )
            raise StreamPlanError(msg)

        return value

    def _pipe_bytes(self, node: DagNode) -> int:
        value = node.args.get(self.PIPE_FIELD, self._default_pipe_bytes)

        if isinstance(value, bool):
            raise StreamPlanError(self._pipe_text(node, value))

        if not isinstance(value, int):
            raise StreamPlanError(self._pipe_text(node, value))

        if value < 0:
            raise StreamPlanError(self._pipe_text(node, value))

        return value

    def _pipe_text(self, node: DagNode, value: JsonValue) -> str:
        return (
            f"stream plan: {node.label()} field {self.PIPE_FIELD!r} expects a "
            f"non-negative integer, got {value!r}"
        )


class WorkflowNodeResult(BaseModel):
    """Итог узла workflow: какой инструмент с чем вызван и чем он кончился.

    key — имя узла, данное моделью; call_id — идентификатор вызова узла, под
    которым идут его журнал и шаг ленты. errored — вызов кончился ошибкой
    самого вызова (аргументы, права), а не результатом инструмента.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    call_id: str
    tool: str
    args: Mapping[str, Any]
    errored: bool
    content: str
    result: ToolResult


class WorkflowResult(ToolResultBase):
    """Итог вызова workflow: результаты его узлов в порядке описания.

    Модель описывает связку инструментов одним вызовом workflow; исполнитель
    DAG отдаёт итог каждого узла, а этот результат несёт их модели и
    истории одним конвертом. Клиент оживляет его по kind и раскрывает в
    шаги узлов.
    """

    kind: Literal["workflow"] = "workflow"
    nodes: Sequence[WorkflowNodeResult]

    def llm_view(self) -> str:
        parts: list[str] = []
        for node in self.nodes:
            parts.append(f"[{node.key}] {node.tool}:\n{node.content}")

        return "\n\n".join(parts)

    def chat_view(self) -> ChatView:
        return ChatView(markdown=FactsBlock(facts=self._facts()).markdown())

    def _facts(self) -> list[Fact]:
        facts: list[Fact] = []
        for node in self.nodes:
            status = "ok"
            if not node.result.ok:
                status = "failed"

            facts.append(Fact(key=f"{node.key} ({node.tool})", value=status))

        return facts


@dataclass(frozen=True)
class ToolHints:
    """Пометки инструмента по стандартным annotations MCP: читает ли он
    только (readOnlyHint) и может ли разрушить данные (destructiveHint).

    По ним клиент решает, спрашивать ли подтверждение перед вызовом.
    Инструмент без пометок считается меняющим и разрушающим — так же
    трактует отсутствие annotations стандарт MCP.
    """

    read_only: bool = False
    destructive: bool = True


@dataclass(frozen=True)
class ToolCard:
    """Карточка инструмента для клиента порта: имя, описание, JSON-схема
    вызова — та, что видит модель после всех обвязок, — вид аргументов
    вызова для ленты клиента и пометки; views None — вида у инструмента
    нет (чужой сервер, встроенный узел), и лента покажет аргументы
    json-текстом."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    views: Mapping[str, FieldView] | None
    hints: ToolHints = field(default_factory=ToolHints)


class JournalAddress(BaseModel):
    """Где лежит журнал вызова: сервер клиента, запуск на нём и
    идентификатор вызова, под которым сервер ведёт журнал.

    Адрес несёт итог вызова, который исполнил сервер с журналом; клиент
    хранит его вместе с итогом в истории, и журнал читается после конца хода.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    server: str = Field(min_length=1)
    run: str = Field(min_length=1)
    call: str = Field(min_length=1)


class NodeReports:
    """Подпись отчёта о ходе работы узлом связки и её разбор.

    Связка идёт одним запросом, а уведомление прогресса несёт только число
    и текст: чей это отчёт, говорит начало текста — ключ узла, который ему
    дал автор связки. Сервер зовёт labeled(), клиент — split(): по ключу он
    отдаёт отчёт шагу своего вызова. Обе стороны держат формат здесь.
    """

    SEPARATOR: ClassVar[str] = ": "

    def labeled(self, key: str, text: str) -> str:
        """Текст отчёта узла key."""
        return f"{key}{self.SEPARATOR}{text}"

    def split(self, text: str) -> tuple[str, str] | None:
        """Ключ узла и его текст; None — отчёт узлом не подписан."""
        key, separator, rest = text.partition(self.SEPARATOR)
        if not separator:
            return None

        return key, rest


class NodeOutcome(BaseModel):
    """Итог узла: текст для модели и результат семейства; сбой — FailureResult.

    errored — вызов кончился ошибкой самого вызова: инструмент поднял
    исключение до тела (аргументы, права) либо сервер отказал вызову до
    старта. journal — адрес журнала вызова на сервере, который его исполнил;
    None — вызов исполнен в своём процессе либо сервер журнала не ведёт.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    tool: str
    content: str
    artifact: ToolResult
    errored: bool = False
    journal: JournalAddress | None = None
    media: Mapping[str, Mapping[str, str]] | None = None
    """Типы содержимого аргументов по идентификаторам вызовов: самого
    вызова, а у связки — каждого её узла. Их называет порт сервера, чьи
    инструменты описаны схемой; None — вызов исполнен в своём процессе, и
    его аргументы показывает модель вызова инструмента."""

    def failed(self) -> bool:
        return isinstance(self.artifact, FailureResult)

    def ok(self) -> bool:
        """Вызов дошёл до тела, и тело вернуло удачный результат."""
        if self.errored:
            return False

        return self.artifact.ok

    def error_text(self) -> str:
        """Текст отказа для журнала; пустой — вызов удался."""
        if self.errored:
            return self.content

        if not self.artifact.ok:
            return self.artifact.llm_view()

        return ""


class NodeOutcomes:
    """Фабрика итогов вызова: единственное место, где результат семейства
    становится итогом NodeOutcome.

    Создаётся каждым, кто отвечает на вызов узла: исполнителем узлов
    (DagRunner), портами ToolServer (свой исполнитель, маршрут по серверам,
    запечатывание, клиент MCP) и стыком приложения с графом. Текст для модели
    берётся у самого результата.
    """

    def of(self, call: DagNode, result: ToolResultBase, errored: bool) -> NodeOutcome:
        """Итог вызова call с результатом result; errored — вызов кончился
        ошибкой самого вызова, а не ответом тела."""
        return self._built(call.key, call.tool, result, errored)

    def refused(self, call: DagNode, failure: FailureResult) -> NodeOutcome:
        """Отказ вызову call: до тела он не дошёл либо тело сорвалось."""
        return self._built(call.key, call.tool, failure, True)

    def unnamed(self, key: str, tool: str, failure: FailureResult) -> NodeOutcome:
        """Отказ вызову инструмента tool, который узлом не назвать: у него
        нет идентификатора вызова; key — что клиент прислал на его месте."""
        return self._built(key, tool, failure, True)

    @staticmethod
    def _built(
        key: str, tool: str, result: ToolResultBase, errored: bool
    ) -> NodeOutcome:
        content, artifact = result.packed()

        return NodeOutcome(
            key=key, tool=tool, content=content, artifact=artifact, errored=errored
        )

    def settled(self, outcome: NodeOutcome) -> asyncio.Future[NodeOutcome]:
        """Готовое ожидание итога, который известен без исполнения."""
        done: asyncio.Future[NodeOutcome] = asyncio.get_running_loop().create_future()
        done.set_result(outcome)

        return done


NodeBody: TypeAlias = Callable[[DagNode], Awaitable[NodeOutcome]]
"""Тело узла: вызов инструмента узла и его итог. Тело не бросает: сбой
вызова оно отдаёт итогом-отказом (errored); наверх идёт только отмена."""


class NodeCalls(Protocol):
    """Путь вызова узла к телу его инструмента.

    Исполнитель (DagRunner, порт удалённого сервера) не зовёт тело узла
    сам, а отдаёт вызов сюда. Сервис исполняет тело напрямую (DirectCalls);
    приложение с лентой проводит вызов через свой компонент, который
    сообщает ленте о начале и конце вызова. Тело узла не бросает: сбой
    вызова приходит наблюдателю итогом-отказом, как и удача.
    """

    @abstractmethod
    async def conducted(
        self, card: ToolCard, node: DagNode, body: NodeBody
    ) -> NodeOutcome:
        """Итог вызова узла node инструмента card, исполненного телом body."""


class CallDag:
    """Описание DAG по вызовам клиента порта.

    Вызов приходит узлом DagNode: ключ — идентификатор вызова. Обычный вызов
    — DAG из одного этого узла. Узлы вызова workflow — уже узлы описания DAG (DagSpec);
    ключ узла заменяется на идентификатор вызова узла (под ним идут журнал и
    шаг ленты узла): заданный клиентом call_id, иначе идентификатор вызова
    workflow с номером узла. Имя, данное узлу моделью, едет в title — им
    узел называется в текстах отказов. Создаётся исполнителем порта
    (LocalDagService) и клиентами, которым нужны узлы связки до отправки:
    клиент MCP раскладывает связку на вызовы узлов, запечатывание меняет
    аргументы узлов.
    """

    WORKFLOW: ClassVar[str] = "workflow"
    """Имя инструмента-связки: узлы его вызова — вызовы других инструментов."""

    NODES: ClassVar[str] = "nodes"

    def of(self, call: DagNode) -> DagSpec:
        """DAG одного вызова.

        Ошибки:
        StreamPlanError — вызов workflow не проходит форму описания DAG.
        """
        if call.tool != self.WORKFLOW:
            return DagSpec(name=call.key, version=1, nodes=[call])

        return DagSpec(name=call.key, version=1, nodes=self.nodes_of(call))

    def described(self, call: DagNode) -> Sequence[DagNode]:
        """Узлы вызова workflow, как их назвала модель.

        Ошибки:
        StreamPlanError — вызов workflow не проходит форму описания DAG.
        """
        raw: dict[str, object] = {"name": call.key, "version": 1}
        if self.NODES in call.args:
            raw[self.NODES] = call.args[self.NODES]

        try:
            return DagSpec.model_validate(raw).nodes
        except ValidationError as exc:
            msg = (
                f"workflow call {call.key!r} does not match its schema: "
                f"{ValidationText.of(exc)}"
            )
            raise StreamPlanError(msg) from exc

    def nodes_of(self, call: DagNode) -> list[DagNode]:
        """Узлы вызова workflow вызовами своих инструментов: ключ узла —
        идентификатор его вызова, имя от модели — в title.

        Ошибки:
        StreamPlanError — вызов workflow не проходит форму описания DAG.
        """
        return list(self._called(call.key, self.described(call)))

    def with_nodes(self, call: DagNode, nodes: Sequence[DagNode]) -> DagNode:
        """Тот же вызов workflow с узлами nodes на месте описанных."""
        described: list[JsonValue] = []
        for node in nodes:
            described.append(node.model_dump(mode="json", exclude_defaults=True))

        args = dict(call.args)
        args[self.NODES] = described

        return call.model_copy(update={"args": args})

    def title_of(self, call: DagNode, node_id: str) -> str | None:
        """Имя, которое автор связки дал узлу с идентификатором вызова
        node_id; None — такого узла в вызове workflow нет.

        Ошибки:
        StreamPlanError — вызов workflow не проходит форму описания DAG.
        """
        for index, node in enumerate(self.described(call)):
            if node.call_id == node_id:
                return node.key

            if node.call_id:
                continue

            if node_id.endswith(self._suffix(index)):
                return node.key

        return None

    @staticmethod
    def _suffix(index: int) -> str:
        """Хвост идентификатора вызова узла, которому автор его не назвал."""
        return f"_{index}"

    def _called(self, call_id: str, described: Sequence[DagNode]) -> Iterator[DagNode]:
        for index, node in enumerate(described):
            key = node.call_id
            if not key:
                key = f"{call_id}{self._suffix(index)}"

            yield node.model_copy(update={"key": key, "title": node.key})


class ToolServer(Protocol):
    """Порт сервера инструментов для клиента.

    Клиент (чат, сервис MCP) знает исполнение только через него: берёт
    карточки инструментов, которые сервер отдаёт модели, читает объявленные
    сервером возможности и шлёт вызовы. Вызов — узел DagNode: ключ узла —
    идентификатор вызова; итог — NodeOutcome с тем же ключом. Что стоит за
    портом, клиенту неизвестно: исполнитель своего процесса
    (LocalDagService), несколько серверов с маршрутом по имени (ToolServers)
    либо обёртка клиента над другим портом.
    """

    @abstractmethod
    def tools(self) -> Sequence[ToolCard]:
        """Карточки инструментов, которые сервер отдаёт модели."""

    @abstractmethod
    def features(self) -> Mapping[str, Mapping[str, object]]:
        """Возможности, которые сервер объявляет клиенту при подключении:
        идентификатор возможности → её настройки."""

    @abstractmethod
    async def submit(
        self, calls: Sequence[DagNode]
    ) -> Sequence[asyncio.Future[NodeOutcome]]:
        """Принять вызовы одного ответа модели; итог каждого — своё ожидание.

        Ожидания идут в порядке вызовов и кончаются независимо: быстрый
        вызов отвечает, не дожидаясь медленного соседа. Между собой вызовы
        пакета не связаны: каждый идёт своим запуском. Отмена ожидания
        останавливает его вызов.
        """

    async def call(self, call: DagNode) -> NodeOutcome:
        """Исполнить один вызов и дождаться его итога."""
        pending = await self.submit([call])

        return await pending[0]

    def names(self) -> frozenset[str]:
        """Имена инструментов, которые сервер отдаёт модели."""
        names: list[str] = []
        for card in self.tools():
            names.append(card.name)

        return frozenset(names)
