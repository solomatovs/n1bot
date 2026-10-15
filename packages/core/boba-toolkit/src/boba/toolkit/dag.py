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
результаты узлов одним конвертом; клиенту MCP он уходит блоками узлов с
заголовками и списком узлов (NestedCallsOutput) в structuredContent.

Здесь же контракт порта сервера инструментов — типы, которыми клиент порта
(чат, сервис MCP, клиент MCP) и его исполнитель говорят друг с другом:
карточка инструмента ToolCard, итог вызова NodeOutcome с фабрикой
NodeOutcomes и адресом журнала JournalAddress, путь вызова узла к телу
NodeCalls, сам порт ToolServer и ToolServers — несколько серверов за
одним портом с маршрутом по имени инструмента; ServerFeature — базовый
класс настроек расширений, которые сервер объявляет клиенту. CallDag строит
описание DAG по вызовам клиента порта, называет инструмент-связку и
объявляет его форму расширением «вложенные вызовы» (NestedCallsFeature): по
нему клиент разбирает узлы связки, не зная инструмента. Остальные
реализации порта живут в boba.toolrun и клиенте MCP.

Ошибки:
StreamPlanError — поле порта узла не имя канала, pipe_bytes не число либо
    граф нарушен (правила StreamPlan); вызов workflow не проходит форму
    описания DAG; аргументы связки не по объявленной форме расширения.
ToolServersError — два сервера за одним портом отдают инструмент с одним
    именем.
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Literal, Protocol, Self, TypeAlias

import mcp_types as mt
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
from boba.toolkit.failure import InvokeErrorKind, ValidationText
from boba.toolkit.ports import PortDecl, StreamSpec
from boba.toolkit.result import (
    ChatView,
    ErrorResult,
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
    "NestedCallReply",
    "NestedCallsFeature",
    "NestedCallsOutput",
    "NodeBody",
    "NodeCalls",
    "NodeOutcome",
    "NodeOutcomes",
    "NodeReports",
    "NodeStatus",
    "ServerFeature",
    "ToolCard",
    "ToolHints",
    "ToolServer",
    "ToolServers",
    "ToolServersError",
    "ToolUseResult",
    "ToolUseResultKey",
    "ToolUseResults",
    "WorkflowField",
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


class NodeStatus(StrEnum):
    """Статус узла связки в заголовке его блоков и в списке узлов итога."""

    OK = "ok"
    FAILED = "failed"


class WorkflowNodeResult(BaseModel):
    """Итог узла workflow: какой инструмент вызван и чем он кончился.

    key — имя узла, данное моделью; call_id — идентификатор вызова узла, под
    которым идёт его журнал. errored — вызов кончился ошибкой самого вызова
    (аргументы, права), а не результатом инструмента. Поля, которых модель
    больше не знает (аргументы узла в истории прежних версий), при
    разборе пропускаются: старые треды открываются.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    key: str
    call_id: str
    tool: str
    errored: bool
    content: str
    result: ToolResult

    @property
    def status(self) -> NodeStatus:
        if not self.result.ok:
            return NodeStatus.FAILED

        return NodeStatus.OK

    def heading(self) -> str:
        """Заголовок узла перед его блоками: ключ, инструмент и статус."""
        return f"[{self.key}] {self.tool} — {self.status.value}"


class NestedCallReply(BaseModel):
    """Узел в итоге инструмента со вложенными вызовами, как он едет клиенту.

    key, call_id, tool — те же, что у итога узла (WorkflowNodeResult):
    имя узла от автора связки, идентификатор вызова узла (под ним сервер
    ведёт журнал узла) и инструмент на сервере. blocks — номера блоков узла
    в content ответа, без его заголовка; result — модель результата узла с
    kind, по ней клиент показывает узел подробнее текста.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    call_id: str
    tool: str
    status: NodeStatus
    blocks: Sequence[int]
    result: Mapping[str, JsonValue]


class NestedCallsOutput(BaseModel):
    """Собственные данные итога связки для structuredContent: узлы в
    порядке описания. Строит её WorkflowResult.structured; сведения о
    вызове к ней добавляет NestedCallsReply (boba.toolkit.wire)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: Sequence[NestedCallReply]


class WorkflowResult(ToolResultBase):
    """Итог вызова workflow: результаты его узлов в порядке описания.

    Модель описывает связку инструментов одним вызовом workflow; исполнитель
    DAG отдаёт итог каждого узла, а этот результат несёт их модели и
    истории одним конвертом. Клиенту MCP он уходит блоками: перед блоками
    каждого узла — его заголовок; список узлов с номерами их блоков —
    в structuredContent.
    """

    kind: Literal["workflow"] = "workflow"
    nodes: Sequence[WorkflowNodeResult]

    def llm_view(self) -> str:
        parts: list[str] = []
        for node in self.nodes:
            parts.append(f"{node.heading()}\n{node.content}")

        return "\n".join(parts)

    def chat_view(self) -> ChatView:
        return ChatView(markdown=FactsBlock(facts=self._facts()).markdown())

    def disclosed(self) -> Self:
        nodes: list[WorkflowNodeResult] = []
        for node in self.nodes:
            nodes.append(node.model_copy(update={"result": node.result.disclosed()}))

        return self.model_copy(update={"nodes": nodes})

    def blocks(self) -> list[mt.ContentBlock]:
        blocks: list[mt.ContentBlock] = []
        for node in self.nodes:
            blocks.append(mt.TextContent(text=node.heading()))
            blocks.extend(node.result.blocks())

        return blocks

    def structured(self) -> dict[str, JsonValue] | None:
        replies: list[NestedCallReply] = []
        position = 0
        for node in self.nodes:
            position += 1
            count = len(node.result.blocks())
            replies.append(
                NestedCallReply(
                    key=node.key,
                    call_id=node.call_id,
                    tool=node.tool,
                    status=node.status,
                    blocks=list(range(position, position + count)),
                    result=node.result.model_dump(mode="json"),
                )
            )
            position += count

        return NestedCallsOutput(nodes=replies).model_dump(mode="json")

    def _facts(self) -> list[Fact]:
        facts: list[Fact] = []
        for node in self.nodes:
            fact = Fact(key=f"{node.key} ({node.tool})", value=node.status.value)
            facts.append(fact)

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
    вызова для ленты клиента, схема итога (outputSchema) и пометки; views
    None — вида у инструмента нет (чужой сервер, встроенный узел), и лента
    покажет аргументы json-текстом; output None — схемы итога у
    инструмента нет."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    views: Mapping[str, FieldView] | None
    output: Mapping[str, Any] | None
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
    """Подпись отчёта о ходе работы узлом связки.

    Связка идёт одним запросом, а уведомление прогресса несёт только число
    и текст: чей это отчёт, говорит начало текста — ключ узла, который ему
    дал автор связки. Сервер подписывает им отчёты узлов (labeled).
    """

    SEPARATOR: ClassVar[str] = ": "

    def labeled(self, key: str, text: str) -> str:
        """Текст отчёта узла key."""
        return f"{key}{self.SEPARATOR}{text}"


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
    """Типы содержимого аргументов по идентификаторам вызовов. Их называет
    порт сервера, чьи инструменты описаны схемой; None — вызов исполнен в
    своём процессе, и его аргументы показывает модель вызова инструмента."""
    reply: Mapping[str, JsonValue] | None = None
    """Ответ сервера MCP на вызов как пришёл; None — порт его не хранит."""

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

    def use_result(self) -> ToolUseResult:
        """Итог в форме записи истории: ответ сервера MCP как пришёл, а без
        него — результат семейства; адрес журнала и типы содержимого
        аргументов."""
        artifact: Any = self.artifact.model_dump(mode="json", exclude_none=True)
        if self.reply is not None:
            artifact = self.reply

        return ToolUseResult(artifact=artifact, journal=self.journal, media=self.media)


class ToolUseResultKey(StrEnum):
    """Ключи записи `toolUseResult` итога вызова в истории агента."""

    ARTIFACT = "artifact"
    JOURNAL = "journal"
    MEDIA = "media"


class ToolUseResult(BaseModel):
    """Итог вызова, каким его хранит история агента и несёт `rawOutput`
    уведомления `tool_call_update`: результат семейства словарём (artifact),
    адрес журнала вызова на сервере (journal) и типы содержимого аргументов
    по идентификаторам вызовов (media). Пишет агент по NodeOutcome.use_result
    и скрипт миграции истории; читает лента чата (ToolUseResults)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact: Any = None
    journal: JournalAddress | None = None
    media: Mapping[str, Mapping[str, str]] | None = None


class ToolUseResults:
    """Разбор `rawOutput` в ToolUseResult: запись с ключами ToolUseResultKey
    проверяется моделью, любое другое значение (текст отказа, результат
    стороннего инструмента) становится артефактом как есть.

    Ошибки:
    ValidationError — запись с ключами итога не проходит модель.
    """

    def of(self, raw: Any) -> ToolUseResult:
        if not isinstance(raw, Mapping):
            return ToolUseResult(artifact=raw)

        known = {key.value for key in ToolUseResultKey}
        if not set(raw) <= known:
            return ToolUseResult(artifact=raw)

        return ToolUseResult.model_validate(raw)


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


class WorkflowField(StrEnum):
    """Имена полей аргумента вызова workflow, как их несёт сервер: список
    узлов и поля узла — те же, что у DagNode."""

    NODES = "nodes"
    KEY = "key"
    TOOL = "tool"
    ARGS = "args"


class CallDag:
    """Описание DAG по вызовам клиента порта.

    Вызов приходит узлом DagNode: ключ — идентификатор вызова. Обычный вызов
    — DAG из одного этого узла. Узлы вызова workflow — уже узлы описания DAG (DagSpec);
    ключ узла заменяется на идентификатор вызова узла (под ним идёт журнал
    узла): заданный клиентом call_id, иначе идентификатор вызова workflow с
    номером узла. Имя, данное узлу моделью, едет в title — им
    узел называется в текстах отказов. Создаётся исполнителем порта
    (LocalDagService): он же объявляет клиентам форму вызова workflow
    расширением feature(), по которому клиент меняет аргументы узлов до
    отправки, не зная инструмента.
    """

    WORKFLOW: ClassVar[str] = "workflow"
    """Имя инструмента-связки: узлы его вызова — вызовы других инструментов."""

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
        if WorkflowField.NODES in call.args:
            raw[WorkflowField.NODES] = call.args[WorkflowField.NODES]

        try:
            return DagSpec.model_validate(raw).nodes
        except ValidationError as exc:
            msg = (
                f"workflow call {call.key!r} does not match its schema: "
                f"{ValidationText.of(exc)}"
            )
            raise StreamPlanError(msg) from exc

    def feature(self) -> NestedCallsFeature:
        """Объявление формы вызова workflow, которую разбирает described()."""
        return NestedCallsFeature(
            tool=self.WORKFLOW,
            nodes=WorkflowField.NODES,
            node_key=WorkflowField.KEY,
            node_tool=WorkflowField.TOOL,
            node_args=WorkflowField.ARGS,
        )

    def nodes_of(self, call: DagNode) -> list[DagNode]:
        """Узлы вызова workflow вызовами своих инструментов: ключ узла —
        идентификатор его вызова, имя от модели — в title.

        Ошибки:
        StreamPlanError — вызов workflow не проходит форму описания DAG.
        """
        return list(self._called(call.key, self.described(call)))

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


class ServerFeature(BaseModel):
    """Базовый класс настроек расширения, которое сервер инструментов
    объявляет клиенту при подключении (ToolServer.features).

    Наследники — JournalFeature, FilesFeature (boba.toolkit.wire) и
    NestedCallsFeature: ID называет расширение, поля — его настройки. Сервер
    кладёт в объявление settings(), клиент разбирает его моделью наследника
    один раз при подключении.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ID: ClassVar[str]

    def settings(self) -> dict[str, object]:
        """Настройки расширения, как они едут клиенту."""
        return self.model_dump(mode="json")


class NestedCallsFeature(ServerFeature):
    """Настройки расширения сервера «вложенные вызовы».

    Инструмент tool несёт в аргументе nodes список вызовов других
    инструментов сервера; у каждого элемента списка поля node_key, node_tool
    и node_args — ключ узла, имя инструмента и его аргументы. Сервер
    объявляет форму своего инструмента-связки (CallDag.feature), клиент по
    ней разбирает узлы вызова (calls) и собирает вызов обратно с другими
    аргументами узлов (replaced) — например, с запечатанными соединениями.
    """

    ID: ClassVar[str] = "com.boba/nested-calls"

    tool: str = Field(min_length=1)
    nodes: str = Field(min_length=1)
    node_key: str = Field(min_length=1)
    node_tool: str = Field(min_length=1)
    node_args: str = Field(min_length=1)

    def calls(self, arguments: Mapping[str, JsonValue]) -> Sequence[DagNode]:
        """Узлы вызова tool с аргументами arguments.

        Ошибки:
        StreamPlanError — аргумент nodes не список объектов либо узел не
            проходит форму узла.
        """
        listed = arguments.get(self.nodes)
        if not isinstance(listed, list):
            msg = (
                f"{self.tool} call: argument {self.nodes!r} expects a list of "
                f"node objects, got {listed!r}"
            )
            raise StreamPlanError(msg)

        calls: list[DagNode] = []
        for index, node in enumerate(listed):
            if not isinstance(node, dict):
                msg = (
                    f"{self.tool} call: node #{index} of {self.nodes!r} expects "
                    f"an object, got {node!r}"
                )
                raise StreamPlanError(msg)

            undeclared = sorted(
                set(node) - {self.node_key, self.node_tool, self.node_args}
            )
            if undeclared:
                msg = (
                    f"{self.tool} call: node #{index} of {self.nodes!r} carries "
                    f"fields outside the declared shape: {', '.join(undeclared)}"
                )
                raise StreamPlanError(msg)

            raw = {
                WorkflowField.KEY: node.get(self.node_key),
                WorkflowField.TOOL: node.get(self.node_tool),
                WorkflowField.ARGS: node.get(self.node_args),
            }
            try:
                calls.append(DagNode.model_validate(raw))
            except ValidationError as exc:
                msg = (
                    f"{self.tool} call: node #{index} of {self.nodes!r} expects "
                    f"{self.node_key!r}, {self.node_tool!r} and an object "
                    f"{self.node_args!r}: {ValidationText.of(exc)}"
                )
                raise StreamPlanError(msg) from exc

        return calls

    def replaced(
        self, arguments: Mapping[str, JsonValue], calls: Sequence[DagNode]
    ) -> dict[str, JsonValue]:
        """Те же аргументы вызова tool, узлы которого пересобраны из calls
        по объявленной форме; прочие аргументы вызова как есть."""
        nodes: list[JsonValue] = []
        for call in calls:
            nodes.append(
                {
                    self.node_key: call.key,
                    self.node_tool: call.tool,
                    self.node_args: dict(call.args),
                }
            )

        replaced = dict(arguments)
        replaced[self.nodes] = nodes

        return replaced


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


class ToolServersError(RuntimeError):
    """Серверы за одним портом нельзя собрать: имена инструментов совпали."""


class UnknownTool:
    """Отказ вызову с именем, которого у сервера нет: называет, какие есть."""

    def __init__(self, known: frozenset[str]) -> None:
        self._known = known

    def refusal(self, name: str) -> ErrorResult:
        msg = (
            f"tool {name!r} does not exist; the available tools are "
            f"{sorted(self._known)}"
        )

        return ErrorResult(message=msg, error_kind=InvokeErrorKind.UNKNOWN_TOOL)


class ToolServers(ToolServer):
    """Несколько серверов инструментов за одним портом.

    Клиент видит один порт: инструменты и возможности всех серверов вместе,
    вызов уходит тому серверу, чей это инструмент. Маршрут — имя
    инструмента; вызовы пакета к одному серверу идут ему одним пакетом.
    Выдуманное имя получает отказ со списком всех инструментов. Собирает
    его реестр инструментов (ToolRegistry.server).
    """

    def __init__(self, servers: Sequence[ToolServer]) -> None:
        self._servers = tuple(servers)
        self._owner: dict[str, int] = {}
        for index, server in enumerate(self._servers):
            for card in server.tools():
                self._claim(card.name, index)

        self._unknown = UnknownTool(frozenset(self._owner))
        self._outcomes = NodeOutcomes()

    def tools(self) -> Sequence[ToolCard]:
        offered: list[ToolCard] = []
        for server in self._servers:
            offered.extend(server.tools())

        return offered

    def features(self) -> Mapping[str, Mapping[str, object]]:
        declared: dict[str, Mapping[str, object]] = {}
        for server in self._servers:
            declared.update(server.features())

        return declared

    async def submit(
        self, calls: Sequence[DagNode]
    ) -> Sequence[asyncio.Future[NodeOutcome]]:
        routed: dict[int, list[int]] = {}
        pending: dict[int, asyncio.Future[NodeOutcome]] = {}
        for position, call in enumerate(calls):
            owner = self._owner.get(call.tool)
            if owner is None:
                refusal = self._unknown.refusal(call.tool)
                refused = self._outcomes.refused(call, refusal)
                pending[position] = self._outcomes.settled(refused)
                continue

            routed.setdefault(owner, []).append(position)

        for owner, positions in routed.items():
            batch: list[DagNode] = []
            for position in positions:
                batch.append(calls[position])

            accepted = await self._servers[owner].submit(batch)
            for position, future in zip(positions, accepted, strict=True):
                pending[position] = future

        ordered: list[asyncio.Future[NodeOutcome]] = []
        for position in range(len(calls)):
            ordered.append(pending[position])

        return ordered

    def _claim(self, name: str, index: int) -> None:
        if name in self._owner:
            msg = (
                f"tool servers: tool {name!r} is offered by two servers "
                f"(#{self._owner[name]} and #{index}); a tool name routes to "
                "exactly one server"
            )
            raise ToolServersError(msg)

        self._owner[name] = index
