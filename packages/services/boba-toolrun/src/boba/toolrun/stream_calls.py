"""Связывание потоковых инструментов моделью: каналы в аргументах вызовов.

Модель запускает насосы и трансформы одним ответом и связывает их именами
каналов: выходной порт вызова — поле `out="orders"`, входной — поле
`feed="orders"` или список `feeds=["a", "b"]`. Здесь три части этого пути:

- StreamChannelFields заменяет порты в схеме модели полями каналов, барьер
  группы StreamGroup из схемы убирает.
- StreamGroups строит по всему ответу модели план каналов и группу вызовов
  (boba.toolkit.chain) и раздаёт их ручки по tool_call_id.
- StreamCallHooks — обвязка вызова: ставит ручку группы в PipelineSlot,
  чтобы обёртка запуска открыла вызов потоково, а сбой вызова раньше тела
  (права, соединение, конфиг) срывает всю группу.

Ошибки:
StreamGroupsConfigError — секции [stream_groups] нет в конфиге.
PayloadFailureError — вызов потокового инструмента отвергнут: план каналов
    его ответа нарушен либо плана для вызова нет; текст идёт модели.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Protocol

from langchain_core.messages import ToolCall
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from boba.toolkit.chain import (
    PipelineSlot,
    StreamGroupRun,
    StreamInput,
    StreamNode,
    StreamPlan,
    StreamPlanError,
    StreamTimings,
)
from boba.toolkit.entry import ToolArgv
from boba.toolkit.failure import FailurePacker
from boba.toolkit.launcher import PayloadFailureError
from boba.toolkit.ports import PortDecl, PortDirection, StreamSpec, ToolStreamSpecs
from boba.toolkit.result import ErrorResult, FailureResult
from boba.toolrun.call_id import ToolCallIdField
from boba.toolrun.wrapping import CallHooks, ToolBody, ToolSchema

__all__ = [
    "StreamCallHooks",
    "StreamCallKind",
    "StreamChannelFields",
    "StreamGroups",
    "StreamGroupsConfig",
    "StreamGroupsConfigError",
]

logger = logging.getLogger(__name__)


class StreamGroupsConfigError(RuntimeError):
    """Секции сроков групп потоковых вызовов нет в конфиге."""


class StreamCallKind(StrEnum):
    """Kind отказа потокового вызова до его запуска."""

    PLAN_REFUSED = "stream_plan_refused"
    UNPLANNED = "stream_call_unplanned"


class StreamGroupsConfig(BaseModel):
    """Секция конфига [stream_groups]: сроки группы связанных вызовов."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    SECTION: ClassVar[str] = "stream_groups"

    open_sec: float = Field(
        gt=0, description="Сколько ждать, пока откроются все вызовы группы."
    )
    stall_sec: float = Field(
        gt=0,
        description="Сколько терпеть отсутствие движения данных по открытым каналам.",
    )
    poll_sec: float = Field(gt=0, description="Шаг проверки сроков группы.")

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
            return cls.OUTBOUND

        if port.many:
            return cls.INBOUND_MANY

        return cls.INBOUND


class StreamChannelFields:
    """Поля каналов в схеме модели на месте портов потокового инструмента.

    Порт — канал вызова, значение ему строит гость; модели же нужно назвать
    канал. Поле получает имя порта: одиночный порт — строка с именем канала,
    порт-список — список имён. Барьер группы StreamGroup модели не виден.
    Обёртка запуска поля каналов не сериализует: в её схеме на этих именах
    порты, а их она пропускает.
    """

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

        tool.args_schema = ToolSchema.rebuild(schema, fields, drop)

    @staticmethod
    def _field(port: PortDecl) -> tuple[Any, Any]:
        text = StreamFieldText.of(port).value

        if port.many:
            return (Annotated[list[str], Field(min_length=1, description=text)], ...)

        return (Annotated[str, Field(min_length=1, description=text)], ...)


class StreamEntry(Protocol):
    """Роль вызова в ответе модели: вход в группу либо отказ."""

    def enter(self) -> Callable[[], None]:
        """Войти в вызов; отдаёт уборку. Отказ — PayloadFailureError."""
        ...

    def fail(self, cause: FailureResult) -> None:
        """Вызов сорвался раньше тела: сообщить группе."""
        ...

    def finished(self) -> bool:
        """Роль больше не нужна: её группа кончилась."""
        ...


class GroupEntry(StreamEntry):
    """Вызов группы: ручка узла едет в PipelineSlot на время вызова."""

    def __init__(self, group: StreamGroupRun, key: str) -> None:
        self._group = group
        self._key = key

    def enter(self) -> Callable[[], None]:
        token = PipelineSlot.set(self._group.slot(self._key))

        def leave() -> None:
            PipelineSlot.reset(token)

        return leave

    def fail(self, cause: FailureResult) -> None:
        self._group.refuse(self._key, cause)

    def finished(self) -> bool:
        return self._group.finished()


class RefusedEntry(StreamEntry):
    """Вызов ответа с нарушенным планом каналов: отказ с текстом нарушения."""

    def __init__(self, message: str) -> None:
        self._message = message

    def enter(self) -> Callable[[], None]:
        failure = ErrorResult(
            message=self._message, error_kind=StreamCallKind.PLAN_REFUSED
        )
        raise PayloadFailureError(failure)

    def fail(self, cause: FailureResult) -> None:
        return

    def finished(self) -> bool:
        return False


class UnplannedEntry(StreamEntry):
    """Потоковый вызов, для которого плана нет: вне ответа модели его
    каналам не с кем соединиться."""

    def __init__(self, tool: str, call_id: str) -> None:
        self._tool = tool
        self._call_id = call_id

    def enter(self) -> Callable[[], None]:
        msg = (
            f"tool {self._tool!r} reads or writes stream channels and runs only "
            "in a group of calls of one model response; no channel plan was "
            f"made for call {self._call_id!r}"
        )
        failure = ErrorResult(message=msg, error_kind=StreamCallKind.UNPLANNED)
        raise PayloadFailureError(failure)

    def fail(self, cause: FailureResult) -> None:
        return

    def finished(self) -> bool:
        return True


class PassEntry(StreamEntry):
    """Вызов вне связывания каналов моделью: ничего не меняется."""

    def enter(self) -> Callable[[], None]:
        return self._leave

    def fail(self, cause: FailureResult) -> None:
        return

    def finished(self) -> bool:
        return True

    @staticmethod
    def _leave() -> None:
        return


class StreamGroups:
    """Реестр групп потоковых вызовов текущих ответов модели.

    Middleware агента отдаёт сюда вызовы ответа модели до их исполнения
    (open): потоковые из них собираются в план каналов и группу, каждому
    вызову достаётся роль по его tool_call_id. Аргументы проверяются схемой
    модели сразу — битый вызов отвергает всю группу, а не держит остальных
    до срока открытия. Обвязка StreamCallHooks забирает роль вызова (take).
    Создаётся загрузчиком инструментов и живёт в ToolRegistry.
    """

    def __init__(self, timings: StreamTimings, tools: Sequence[BaseTool]) -> None:
        self._timings = timings
        self._lock = threading.Lock()
        self._entries: dict[str, StreamEntry] = {}
        self._groups: list[StreamGroupRun] = []
        self._schemas: dict[str, type[BaseModel]] = {}
        for tool in tools:
            self._remember(tool)

    def open(self, calls: Sequence[ToolCall]) -> None:
        """План и группа по потоковым вызовам одного ответа модели."""
        streaming = list(self._streaming(calls))
        if not streaming:
            return

        self._purge()

        try:
            plan = StreamPlan(list(self._nodes(streaming)))
        except StreamPlanError as exc:
            self._refuse(streaming, str(exc))
            return

        group = StreamGroupRun(plan, self._timings)
        with self._lock:
            self._groups.append(group)
            for call in streaming:
                key = str(call["id"])
                self._entries[key] = GroupEntry(group, key)

        logger.info("stream group opened: %s", group.labels())

    def take(self, tool: str, call_id: str) -> StreamEntry:
        """Роль вызова; забирается один раз."""
        with self._lock:
            entry = self._entries.pop(call_id, None)

        if entry is None:
            return UnplannedEntry(tool, call_id)

        return entry

    def streaming(self, tool: str) -> bool:
        return tool in self._schemas

    def _remember(self, tool: BaseTool) -> None:
        if not ToolStreamSpecs.of(tool.name).streaming():
            return

        schema = ToolSchema.of(tool)
        if schema is None:
            return

        self._schemas[tool.name] = schema

    def _streaming(self, calls: Sequence[ToolCall]) -> Iterator[ToolCall]:
        for call in calls:
            if call["name"] in self._schemas:
                yield call

    def _nodes(self, calls: Sequence[ToolCall]) -> Iterator[StreamNode]:
        for call in calls:
            yield self._node(call)

    def _node(self, call: ToolCall) -> StreamNode:
        name = call["name"]
        key = call["id"]
        if not key:
            msg = f"stream plan: call of {name!r} has no tool call id"
            raise StreamPlanError(msg)

        try:
            args = self._schemas[name].model_validate(call["args"])
        except ValidationError as exc:
            msg = f"stream plan: call {key!r} of {name!r} has invalid arguments: {exc}"
            raise StreamPlanError(msg) from exc

        spec = ToolStreamSpecs.of(name)

        output: str | None = None
        if outbound := spec.outbound():
            output = str(getattr(args, outbound.name))

        return StreamNode(
            key=key,
            tool=name,
            spec=spec,
            output=output,
            inputs=tuple(self._inputs(spec, args)),
        )

    @staticmethod
    def _inputs(spec: StreamSpec, args: BaseModel) -> Iterator[StreamInput]:
        for port in spec.inbound():
            value = getattr(args, port.name)

            channels: list[str] = [str(value)]
            if port.many:
                channels = list(value)

            for channel in channels:
                yield StreamInput(port=port.name, channel=channel)

    def _refuse(self, calls: Sequence[ToolCall], message: str) -> None:
        logger.warning("stream plan refused: %s", message)

        with self._lock:
            for call in calls:
                key = call["id"]
                if key:
                    self._entries[key] = RefusedEntry(message)

    def _purge(self) -> None:
        """Забыть группы, которые уже кончились, и их невостребованные роли."""
        with self._lock:
            finished: list[StreamGroupRun] = []
            for group in self._groups:
                if group.finished():
                    finished.append(group)

            for group in finished:
                self._groups.remove(group)

            stale: list[str] = []
            for key, entry in self._entries.items():
                if entry.finished():
                    stale.append(key)

            for key in stale:
                del self._entries[key]


@dataclass(frozen=True)
class _EntryScope:
    """Роль вызова на время его исполнения и её уборка."""

    entry: StreamEntry
    leave: Callable[[], None]


class StreamCallHooks(CallHooks[_EntryScope]):
    """Обвязка потокового вызова: роль из StreamGroups на время вызова.

    Ставится снаружи проверки прав: отказ прав, соединения или конфига
    внутри неё приходит сюда ошибкой и срывает группу вызова — остальные
    вызовы группы не ждут его до срока. Вызов, уже поставленный в группу
    своим оркестратором (PipelineSlot занят), проходит как есть.
    """

    PASS: ClassVar[PassEntry] = PassEntry()

    def __init__(self, groups: StreamGroups) -> None:
        self._groups = groups
        self._failures = FailurePacker()

    def guard_all(self, tools: Sequence[BaseTool]) -> None:
        ToolBody.hook_all(tools, self)

    def before(
        self,
        name: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> _EntryScope:
        entry = self._entry_of(name, kwargs)
        return _EntryScope(entry=entry, leave=entry.enter())

    def on_error(self, ctx: _EntryScope, error: Exception) -> object:
        ctx.entry.fail(self._failures.pack(error))
        raise error

    def cleanup(self, ctx: _EntryScope) -> None:
        ctx.leave()

    def _entry_of(self, name: str, kwargs: Mapping[str, object]) -> StreamEntry:
        if not self._groups.streaming(name):
            return self.PASS

        if PipelineSlot.get() is not None:
            return self.PASS

        call_id = kwargs.get(ToolCallIdField.NAME)
        if not isinstance(call_id, str):
            return UnplannedEntry(name, "")

        return self._groups.take(name, call_id)
