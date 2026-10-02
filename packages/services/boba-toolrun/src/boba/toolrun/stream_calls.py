"""Связывание потоковых инструментов моделью: каналы в аргументах вызовов.

Модель запускает насосы и трансформы одним ответом и связывает их именами
каналов: выходной порт вызова — поле `out="orders"`, входной — поле
`feed="orders"` или список `feeds=["a", "b"]`. Здесь три части этого пути:

- StreamChannelFields заменяет порты в схеме модели полями каналов, барьер
  группы StreamGroup из схемы убирает.
- ResponseDag собирает из потоковых вызовов ответа модели описание DAG
  (boba.toolkit.dag): узел — вызов как есть, ключ — tool_call_id.
- StreamRuns — запуски DAG ответов в сессии: первый пришедший вызов ответа
  строит DAG и отдаёт его исполнителю DagRunner, каждый вызов ответа ждёт
  итог своего узла и получает его ToolMessage для модели. Отказ плана и
  вызов вне плана тоже уходят модели сообщением-ошибкой.

Ошибки:
StreamGroupsConfigError — секции [stream_groups] нет в конфиге.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, ClassVar

from langchain_core.messages import AIMessage, ToolCall, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.toolkit.chain import StreamPlanError, StreamTimings
from boba.toolkit.dag import DagNode, DagPlanner, DagSpec
from boba.toolkit.entry import ToolArgv
from boba.toolkit.ports import PortDecl, PortDirection, ToolStreamSpecs
from boba.toolkit.result import ErrorResult, FailureResult
from boba.toolrun.dag_run import DagHandle, DagRunError, DagRunner, NodeOutcome
from boba.toolrun.wrapping import ToolSchema

__all__ = [
    "ResponseDag",
    "StreamCallKind",
    "StreamChannelFields",
    "StreamGroupsConfig",
    "StreamGroupsConfigError",
    "StreamRuns",
]

logger = logging.getLogger(__name__)


class StreamGroupsConfigError(RuntimeError):
    """Секции сроков групп потоковых вызовов нет в конфиге."""


class StreamCallKind(StrEnum):
    """Kind отказа потокового вызова до его запуска."""

    PLAN_REFUSED = "stream_plan_refused"
    UNPLANNED = "stream_call_unplanned"


class StreamGroupsConfig(BaseModel):
    """Секция конфига [stream_groups]: сроки группы связанных вызовов и
    размер буфера пайпов каналов."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    SECTION: ClassVar[str] = "stream_groups"

    PIPE_BYTES_FLOOR: ClassVar[int] = 4096

    open_sec: float = Field(
        gt=0, description="Сколько ждать, пока откроются все вызовы группы."
    )
    stall_sec: float = Field(
        gt=0,
        description="Сколько терпеть отсутствие движения данных по открытым каналам.",
    )
    poll_sec: float = Field(gt=0, description="Шаг проверки сроков группы.")
    pipe_bytes: int = Field(
        ge=PIPE_BYTES_FLOOR,
        description="Буфер пайпа канала, когда вызов его не назвал.",
    )
    pipe_bytes_max: int = Field(
        ge=PIPE_BYTES_FLOOR,
        description="Потолок буфера, который можно запросить полем вызова.",
    )

    @model_validator(mode="after")
    def _max_covers_default(self) -> StreamGroupsConfig:
        if self.pipe_bytes > self.pipe_bytes_max:
            msg = (
                f"[{self.SECTION}]: pipe_bytes {self.pipe_bytes} exceeds "
                f"pipe_bytes_max {self.pipe_bytes_max}"
            )
            raise ValueError(msg)

        return self

    def timings(self) -> StreamTimings:
        return StreamTimings(
            open_sec=self.open_sec, stall_sec=self.stall_sec, poll_sec=self.poll_sec
        )


class StreamFieldText(StrEnum):
    """Описания полей каналов для модели."""

    OUTBOUND = (
        "Имя канала, в который вызов пишет свой поток:\n"
        "   - любое короткое имя, например orders\n"
        "   - читатели называют это же имя в своих входах\n"
        "   - у канала один писатель, читателей сколько угодно\n"
        "       каждый читатель получает поток целиком\n"
        "   - писатель и все его читатели вызываются в одном ответе\n"
    )
    OUTBOUND_MANY = (
        "Имена каналов, которые вызов пишет, списком:\n"
        "   - каждый канал — отдельный поток со своими данными\n"
        "   - у каждого имени свои читатели в этом же ответе\n"
        "   - у канала один писатель, читателей сколько угодно\n"
    )
    INBOUND = (
        "Имя канала, из которого вызов читает поток:\n"
        "   - имя выхода другого вызова этого же ответа\n"
        "   - писатель и читатели вызываются в одном ответе\n"
    )
    INBOUND_MANY = (
        "Имена каналов, которые вызов читает, списком:\n"
        "   - каждое имя — выход другого вызова этого же ответа\n"
        "   - каждый канал приходит отдельным потоком, данные не смешиваются\n"
    )

    @classmethod
    def of(cls, port: PortDecl) -> StreamFieldText:
        if port.direction is PortDirection.OUTBOUND:
            if port.many:
                return cls.OUTBOUND_MANY

            return cls.OUTBOUND

        if port.many:
            return cls.INBOUND_MANY

        return cls.INBOUND


class StreamChannelFields:
    """Поля каналов в схеме модели на месте портов потокового инструмента.

    Порт — канал вызова, значение ему строит гость; модели же нужно назвать
    канал. Поле получает имя порта: одиночный порт — строка с именем канала,
    порт-список — список имён. Писатель каналов получает ещё поле
    pipe_bytes — буфер пайпов своих каналов, с дефолтом и потолком из
    секции [stream_groups]. Барьер группы StreamGroup модели не виден.
    Обёртка запуска поля каналов не сериализует: её схема захвачена до
    пересборки, и этих имён в ней нет.
    """

    PIPE_TEXT: ClassVar[str] = (
        "Буфер пайпа каждого канала этого вызова, в байтах:\n"
        "   - крупнее — быстрее массивная перекачка\n"
        "   - мельче — экономнее общий бюджет пайпов ядра\n"
        "   - действует на выходы вызова и входы их читателей\n"
        "   - ядро не дало запрошенное — группа не запускается\n"
    )

    def __init__(self, config: StreamGroupsConfig) -> None:
        self._config = config

    def attach_all(self, tools: Sequence[BaseTool]) -> None:
        for tool in tools:
            self._attach(tool)

    def _attach(self, tool: BaseTool) -> None:
        schema = ToolSchema.of(tool)
        if schema is None:
            return

        spec = ToolStreamSpecs.of(tool.name)
        groups = ToolArgv.group_fields(schema)
        if not spec.streaming() and not groups:
            return

        fields: dict[str, tuple[Any, Any]] = {}
        drop: list[str] = list(groups)
        for port in spec.ports:
            drop.append(port.name)
            fields[port.name] = self._field(port)

        if spec.outbound():
            fields[DagPlanner.PIPE_FIELD] = self._pipe_field()

        tool.args_schema = ToolSchema.rebuild(schema, fields, drop)

    def _pipe_field(self) -> tuple[Any, Any]:
        declared = Annotated[
            int,
            Field(
                ge=StreamGroupsConfig.PIPE_BYTES_FLOOR,
                le=self._config.pipe_bytes_max,
                description=self.PIPE_TEXT,
            ),
        ]
        return (declared, self._config.pipe_bytes)

    @staticmethod
    def _field(port: PortDecl) -> tuple[Any, Any]:
        text = StreamFieldText.of(port).value

        if port.many:
            return (Annotated[list[str], Field(min_length=1, description=text)], ...)

        return (Annotated[str, Field(min_length=1, description=text)], ...)


class ResponseDag:
    """DAG из ответа модели: потоковые вызовы ответа — узлы, ключ узла —
    tool_call_id, аргументы — как назвала модель. Писатель без pipe_bytes
    получает дефолт секции [stream_groups], как и в его схеме."""

    def __init__(
        self, config: StreamGroupsConfig, streaming: Mapping[str, bool]
    ) -> None:
        self._config = config
        self._streaming = dict(streaming)

    def of(self, response: AIMessage) -> DagSpec:
        """DAG потоковых вызовов ответа.

        Ошибки:
        StreamPlanError — у потокового вызова нет tool_call_id; потоковых
            вызовов в ответе нет.
        """
        nodes = list(self._nodes(response))
        if not nodes:
            msg = "stream plan: the model response has no stream tool calls"
            raise StreamPlanError(msg)

        return DagSpec(name=self.key_of(response), version=1, nodes=nodes)

    @staticmethod
    def key_of(response: AIMessage) -> str:
        """Ключ ответа: его id, без id — id его вызовов."""
        if response.id:
            return response.id

        ids: list[str] = []
        for call in response.tool_calls:
            ids.append(str(call["id"]))

        return "|".join(ids)

    def _nodes(self, response: AIMessage) -> Iterator[DagNode]:
        for call in response.tool_calls:
            if not self._streaming.get(call["name"], False):
                continue

            yield self._node(call)

    def _node(self, call: ToolCall) -> DagNode:
        name = call["name"]
        key = call["id"]
        if not key:
            msg = f"stream plan: call of {name!r} has no tool call id"
            raise StreamPlanError(msg)

        args: dict[str, Any] = dict(call["args"])
        writes = bool(ToolStreamSpecs.of(name).outbound())
        if writes and DagPlanner.PIPE_FIELD not in args:
            args[DagPlanner.PIPE_FIELD] = self._config.pipe_bytes

        return DagNode(key=key, tool=name, args=args)


@dataclass(frozen=True)
class _ResponseRun:
    """Запуск DAG одного ответа: ручка исполнителя либо текст отказа плана."""

    handle: DagHandle | None
    refusal: str


class StreamRuns:
    """Запуски DAG ответов модели в сессии чата.

    Все вызовы ответа модели идут через middleware агента; первый из
    потоковых строит DAG ответа и запускает исполнитель с конфигом своего
    вызова (callbacks ленты), остальные находят готовый запуск. Каждый
    вызов ждёт итог своего узла и получает ToolMessage для модели; отказ
    плана — ошибка каждому вызову ответа, вызов без узла — ошибка ему
    одному. Создаётся на сессию из её инструментов и секции [stream_groups].
    """

    def __init__(self, tools: Sequence[BaseTool], config: StreamGroupsConfig) -> None:
        self._config = config
        self._lock = asyncio.Lock()
        self._runs: dict[str, _ResponseRun] = {}

        by_name: dict[str, BaseTool] = {}
        streaming: dict[str, bool] = {}
        for tool in tools:
            by_name[tool.name] = tool
            streaming[tool.name] = ToolStreamSpecs.of(tool.name).streaming()

        self._streaming = streaming
        self._dags = ResponseDag(config, streaming)
        self._runner = DagRunner(by_name, config.timings())

    def streaming(self, tool: str) -> bool:
        return self._streaming.get(tool, False)

    async def message_for(
        self, response: AIMessage, call: ToolCall, config: RunnableConfig | None
    ) -> ToolMessage:
        """Итог узла вызова call ответа response как сообщение модели."""
        run = await self._run_of(response, config)
        call_id = str(call["id"])

        if run.handle is None:
            failure = ErrorResult(
                message=run.refusal, error_kind=StreamCallKind.PLAN_REFUSED
            )
            return self._failed(call, failure)

        if not run.handle.has(call_id):
            msg = (
                f"tool {call['name']!r} reads or writes stream channels and runs "
                "only in a group of calls of one model response; no channel plan "
                f"was made for call {call_id!r}"
            )
            failure = ErrorResult(message=msg, error_kind=StreamCallKind.UNPLANNED)
            return self._failed(call, failure)

        try:
            outcome = await run.handle.result(call_id)
        except asyncio.CancelledError:
            run.handle.cancel()
            raise

        return self._message(call, outcome)

    async def _run_of(
        self, response: AIMessage, config: RunnableConfig | None
    ) -> _ResponseRun:
        key = ResponseDag.key_of(response)

        async with self._lock:
            run = self._runs.get(key)
            if run is not None:
                return run

            self._purge()
            run = self._start(response, config)
            self._runs[key] = run

        return run

    def _start(
        self, response: AIMessage, config: RunnableConfig | None
    ) -> _ResponseRun:
        try:
            dag = self._dags.of(response)
            handle = self._runner.start(dag, config)
        except (StreamPlanError, DagRunError) as exc:
            logger.warning("stream plan refused: %s", exc)
            return _ResponseRun(handle=None, refusal=str(exc))

        return _ResponseRun(handle=handle, refusal="")

    def _purge(self) -> None:
        """Забыть запуски, которые уже кончились."""
        stale: list[str] = []
        for key, run in self._runs.items():
            if run.handle is None:
                stale.append(key)
                continue

            if run.handle.done():
                stale.append(key)

        for key in stale:
            del self._runs[key]

    @staticmethod
    def _message(call: ToolCall, outcome: NodeOutcome) -> ToolMessage:
        return ToolMessage(
            content=outcome.content,
            artifact=outcome.artifact,
            name=outcome.tool,
            tool_call_id=str(call["id"]),
        )

    @staticmethod
    def _failed(call: ToolCall, failure: FailureResult) -> ToolMessage:
        content, artifact = failure.packed()

        return ToolMessage(
            content=content,
            artifact=artifact,
            name=call["name"],
            tool_call_id=str(call["id"]),
        )
