"""Кадры синхронизации источника и сборка снимка из порций.

Инструмент снятия метаданных в песочнице шлёт в выходной порт три вида
кадров: SyncPlan — что нашёл и сколько объектов, SyncBatch — порцию записей
одной части снимка (тело кадра — JSON-список записей), SyncDone — итог со
счётчиками по частям. Хост складывает порции и по SyncDone собирает снимок
целиком: SnapshotAssembler находит класс снимка по виду в реестре, проверяет,
что счётчики сошлись, и отдаёт снимок с проверенными инвариантами. Части
и их модели объявляет сам класс снимка, здесь про виды ничего не зашито.

Ошибки:
SyncFrameError — кадр или порция не разбираются, счётчики плана и итога не
    сходятся, снимок из порций не проходит инварианты.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, Protocol, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    TypeAdapter,
    ValidationError,
)

from boba.catalog.base import CatalogError
from boba.catalog.sources import (
    SnapshotPart,
    SourceKinds,
    SourceRecord,
    SourceSnapshot,
)

__all__ = [
    "FrameOut",
    "SnapshotAssembler",
    "SnapshotBatches",
    "SyncBatch",
    "SyncDone",
    "SyncEmitter",
    "SyncFrame",
    "SyncFrameError",
    "SyncFrameHead",
    "SyncFrameKind",
    "SyncFrameReceiver",
    "SyncPlan",
]

ReceiveT_co = TypeVar("ReceiveT_co", covariant=True)


class SyncFrameError(CatalogError):
    """Кадр синхронизации не разбирается или порции не складываются в снимок."""


class SyncFrameKind(StrEnum):
    PLAN = "sync.plan"
    BATCH = "sync.batch"
    DONE = "sync.done"


class SnapshotBatches:
    """Тело кадра порции: JSON-список записей одной части снимка."""

    ENCODING: ClassVar[str] = "utf-8"
    HEAD_CHARS: ClassVar[int] = 200

    @classmethod
    def encode(cls, records: Iterable[SourceRecord]) -> bytes:
        dumped: list[dict[str, object]] = []
        for record in records:
            dumped.append(record.model_dump(mode="json"))

        return json.dumps(dumped, ensure_ascii=False).encode(cls.ENCODING)

    @classmethod
    def decode(cls, part: SnapshotPart, body: bytes) -> tuple[SourceRecord, ...]:
        """Записи части из тела кадра.

        Ошибки:
        SyncFrameError — тело не JSON-список записей модели части.
        """
        adapter: TypeAdapter[list[SourceRecord]] = TypeAdapter(list[part.model])
        try:
            records = adapter.validate_json(body)
        except ValidationError as exc:
            head = body[: cls.HEAD_CHARS].decode(cls.ENCODING, errors="replace")
            msg = (
                f"sync batch of part {part.name!r}: body is not a JSON list of "
                f"{part.model.__name__} records, got {head!r}: {exc}"
            )
            raise SyncFrameError(msg) from exc

        return tuple(records)


class SyncFrameReceiver(Protocol[ReceiveT_co]):
    """Приёмник кадров по видам: кадр сам зовёт свой метод через route(),
    приёмник не разбирает вид кадра."""

    @abstractmethod
    def on_plan(self, plan: SyncPlan, body: bytes) -> ReceiveT_co: ...

    @abstractmethod
    def on_batch(self, batch: SyncBatch, body: bytes) -> ReceiveT_co: ...

    @abstractmethod
    def on_done(self, done: SyncDone, body: bytes) -> ReceiveT_co: ...


class SyncPlan(BaseModel):
    """Первый кадр: что инструмент нашёл в источнике и сколько объектов
    (отношений, рутин, последовательностей, типов) он собирается снять."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[SyncFrameKind.PLAN] = SyncFrameKind.PLAN
    source_kind: str = Field(min_length=1)
    database: str = Field(min_length=1)
    schemas: tuple[str, ...]
    objects_total: int = Field(ge=0)
    server_version: str = ""

    def route(
        self, receiver: SyncFrameReceiver[ReceiveT_co], body: bytes
    ) -> ReceiveT_co:
        return receiver.on_plan(self, body)


class SyncBatch(BaseModel):
    """Порция записей одной части снимка; тело кадра — JSON-список записей.
    part — имя части снимка своего вида, objects — сколько объектов стало
    полными этой порцией."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[SyncFrameKind.BATCH] = SyncFrameKind.BATCH
    seq: int = Field(ge=1)
    part: str = Field(min_length=1)
    count: int = Field(ge=0)
    objects: int = Field(ge=0)

    def route(
        self, receiver: SyncFrameReceiver[ReceiveT_co], body: bytes
    ) -> ReceiveT_co:
        return receiver.on_batch(self, body)


class SyncDone(BaseModel):
    """Последний кадр: сколько записей каждой части ушло и сколько объектов."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[SyncFrameKind.DONE] = SyncFrameKind.DONE
    counts: Mapping[str, int]
    objects_total: int = Field(ge=0)
    batches: int = Field(ge=0)

    def route(
        self, receiver: SyncFrameReceiver[ReceiveT_co], body: bytes
    ) -> ReceiveT_co:
        return receiver.on_done(self, body)


SyncFrame = SyncPlan | SyncBatch | SyncDone
"""Кадры порта инструмента: голый союз, как его принимает Outbound."""


class SyncFrameHead(
    RootModel[Annotated[SyncPlan | SyncBatch | SyncDone, Field(discriminator="kind")]]
):
    """Заголовок кадра синхронизации для разбора на хосте: по kind выбирает
    план, порцию или итог; route() ведёт кадр в приёмник."""

    def route(
        self, receiver: SyncFrameReceiver[ReceiveT_co], body: bytes
    ) -> ReceiveT_co:
        return self.root.route(receiver, body)


class FrameOut(Protocol):
    """Выход кадров инструмента: заголовок моделью и тело байтами.
    Реализация в песочнице — Outbound[SyncFrame] из toolkit."""

    @abstractmethod
    def emit(self, head: SyncFrame, body: bytes = b"") -> None: ...


class SyncEmitter:
    """Отправитель кадров на стороне инструмента: нумерует порции, ведёт
    счётчики частей и объектов, шлёт план и итог. Порция объектной части
    (части семейства) продвигает счётчик объектов на число записей."""

    def __init__(self, out: FrameOut, snapshot_class: type[SourceSnapshot]) -> None:
        self._out = out
        self._seq = 0
        self._objects = 0
        self._counts: dict[str, int] = {}
        for part in snapshot_class.parts():
            self._counts[part.name] = 0

        self._object_parts: set[str] = set()
        for family in snapshot_class.FAMILIES:
            self._object_parts.add(family.part)

    @property
    def batches(self) -> int:
        return self._seq

    @property
    def objects(self) -> int:
        return self._objects

    def plan(self, plan: SyncPlan) -> None:
        self._out.emit(plan)

    def batch(self, part: str, records: Sequence[SourceRecord]) -> None:
        """Порция части; пустая не отправляется."""
        if not records:
            return

        objects = 0
        if part in self._object_parts:
            objects = len(records)

        self._seq += 1
        self._counts[part] += len(records)
        self._objects += objects
        head = SyncBatch(seq=self._seq, part=part, count=len(records), objects=objects)
        self._out.emit(head, SnapshotBatches.encode(records))

    def done(self) -> SyncDone:
        done = SyncDone(
            counts=dict(self._counts), objects_total=self._objects, batches=self._seq
        )
        self._out.emit(done)
        return done


class SnapshotAssembler:
    """Накопитель порций на стороне хоста: части складываются по мере
    прихода, по итогу счётчики сверяются с планом и собирается снимок класса
    своего вида."""

    def __init__(self, plan: SyncPlan, kinds: SourceKinds) -> None:
        self._plan = plan
        self._snapshot_class = kinds.snapshot_class(plan.source_kind)
        self._records: dict[str, list[SourceRecord]] = {}
        self._objects = 0
        self._batches = 0
        for part in self._snapshot_class.parts():
            self._records[part.name] = []

    @property
    def plan(self) -> SyncPlan:
        return self._plan

    @property
    def objects_done(self) -> int:
        return self._objects

    @property
    def batches(self) -> int:
        return self._batches

    def part(self, name: str) -> SnapshotPart:
        """Ошибки:
        SyncFrameError — части с таким именем у снимка этого вида нет.
        """
        try:
            return self._snapshot_class.part(name)
        except CatalogError as exc:
            msg = f"sync of {self._plan.source_kind} source: {exc}"
            raise SyncFrameError(msg) from exc

    def take(self, batch: SyncBatch, body: bytes) -> Sequence[SourceRecord]:
        """Порция сложена; возвращает разобранные записи для staging.

        Ошибки:
        SyncFrameError — порция не по порядку, часть неизвестна, тело не
            разбирается или число записей не совпадает с заголовком.
        """
        expected = self._batches + 1
        if batch.seq != expected:
            msg = (
                f"sync batch #{batch.seq} of part {batch.part!r} came out "
                f"of order, expected #{expected}"
            )
            raise SyncFrameError(msg)

        part = self.part(batch.part)
        records = SnapshotBatches.decode(part, body)
        if len(records) != batch.count:
            msg = (
                f"sync batch #{batch.seq} of part {batch.part!r} declares "
                f"{batch.count} records, the body holds {len(records)}"
            )
            raise SyncFrameError(msg)

        self._records[part.name].extend(records)
        self._objects += batch.objects
        self._batches = batch.seq
        return records

    def restore(self, part: str, records: Iterable[SourceRecord]) -> None:
        """Записи из staging при досборке после перезапуска: без порядка порций."""
        self._records[self.part(part).name].extend(records)

    def finish(self, done: SyncDone) -> SourceSnapshot:
        """Снимок из всех порций после сверки итога с планом и порциями.

        Ошибки:
        SyncFrameError — счётчики итога не совпадают с полученным или снимок
            не проходит инварианты.
        """
        for part in self._snapshot_class.parts():
            declared = done.counts.get(part.name, 0)
            got = len(self._records[part.name])
            if declared != got:
                msg = (
                    f"sync done declares {declared} records of part {part.name!r}, "
                    f"{got} arrived in {self._batches} batch(es)"
                )
                raise SyncFrameError(msg)

        if done.objects_total != self._plan.objects_total:
            msg = (
                f"sync done reports {done.objects_total} objects, the plan promised "
                f"{self._plan.objects_total}"
            )
            raise SyncFrameError(msg)

        fields: dict[str, tuple[SourceRecord, ...]] = {}
        for name, records in self._records.items():
            fields[name] = tuple(records)

        try:
            snapshot = self._snapshot_class.model_validate(fields)
            snapshot.check()
        except (ValidationError, CatalogError) as exc:
            kind = self._plan.source_kind
            msg = (
                f"sync of {kind} database {self._plan.database!r}: records of "
                f"{self._batches} batch(es) do not form a valid snapshot: {exc}"
            )
            raise SyncFrameError(msg) from exc

        return snapshot
