"""Синхронизация источника инструментом снятия: запуск инструмента вида
(SourceSnapshot.SYNC_TOOL) от имени субъекта вне чата, приём кадров плана,
порций и итога через FrameTap, staging порций в хранилище и перенос
собранного снимка в версию источника одной транзакцией.

Запуск живёт задачей цикла событий инстанса: SyncRunner держит задачи и
отмены по id синхронизации, cancel() снимает инструмент через RunCancellation
и ждёт закрытия записи. Кадры инструмента приходят в потоке чтения канала
(QueueSink) и передаются в цикл событий очередью; FrameConsumer принимает
их как SyncFrameReceiver: план — в запись синхронизации, порции — в staging
и SnapshotAssembler, итог — в собранный снимок. Инструменты и имена
подключений приходят портами SyncTools и ConnectionDirectory, которые собирает
хост приложения.

Ошибки:
CatalogStoreError — Postgres недоступен или ответ битый.
SourceNotFoundError — источника с таким id нет.
SyncNotFoundError — синхронизации с таким id нет.
SyncRunningError — у источника уже идёт синхронизация.
SyncClosedError — синхронизация уже завершена, отменять нечего.
SyncConnectionNotBoundError — подключение не привязано к источнику.
SyncSetupError — синхронизацию не запустить: у вида нет инструмента снятия,
    инструмент недоступен субъекту, подключение субъекту не видно.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, ClassVar, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from boba.cancellation import RunCancellation, StopReason, ToolStopped
from boba.catalog import (
    CatalogError,
    SnapshotAssembler,
    SourceSnapshot,
    SyncBatch,
    SyncDone,
    SyncFrameError,
    SyncFrameHead,
    SyncFrameReceiver,
    SyncPlan,
)
from boba.catalog_service.records import (
    CatalogServiceError,
    ConnectionEntry,
    Sync,
    SyncClosedError,
    SyncConnectionNotBoundError,
    SyncRequest,
    SyncStatus,
)
from boba.catalog_service.source_store import SourceStore
from boba.identity.context import CallContext, Credential, Initiator, Scope, Subject
from boba.identity.run import RunRegistry
from boba.messaging import ChangeAction
from boba.toolkit.calls import CallIdPrefix
from boba.toolkit.failure import ToolUnavailableError
from boba.toolkit.frames import FrameProtocolError, ToolFrame
from boba.toolkit.launcher import FrameSink, FrameTap
from boba.toolrun.invoke import InvokeReply, ToolInvoker

logger = logging.getLogger(__name__)

__all__ = [
    "ConnectionDirectory",
    "SyncCaller",
    "SyncObserver",
    "SyncPorts",
    "SyncRunner",
    "SyncSetupError",
    "SyncTools",
]

SyncObserver = Callable[[Subject, Sync, ChangeAction], Awaitable[None]]
Progress = Callable[[Sync], Awaitable[None]]


class SyncSetupError(CatalogServiceError):
    """Синхронизацию не запустить: нет инструмента, доступа или подключения."""


class SyncCaller(BaseModel):
    """От чьего имени и откуда запущена синхронизация: субъект, инициатор и
    секреты для инструмента снятия."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    subject: Subject
    initiator: Initiator
    credential: Credential

    def context(self, sync_id: UUID, cancellation: RunCancellation) -> CallContext:
        return CallContext(
            subject=self.subject,
            scope=Scope.job(str(sync_id)),
            initiator=self.initiator,
            credential=self.credential,
            cancellation=cancellation,
        )


class SyncTools(Protocol):
    """Инструменты, видимые субъекту вне чата; собирает хост из реестра."""

    @abstractmethod
    async def invoker(self, subject: Subject) -> ToolInvoker: ...


class ConnectionDirectory(Protocol):
    """Справочник подключений глазами субъекта: список по виду для привязки
    и имя по id для инструмента снятия.

    Ошибки:
    SyncSetupError — подключение субъекту не видно или справочник недоступен.
    """

    @abstractmethod
    async def visible(
        self, subject: Subject, kind: str
    ) -> Sequence[ConnectionEntry]: ...

    @abstractmethod
    async def name_of(self, subject: Subject, connection_id: UUID) -> str: ...


class SyncPorts:
    """Порты синхронизации от хоста: инструменты субъекта и имена подключений."""

    def __init__(self, tools: SyncTools, connections: ConnectionDirectory) -> None:
        self.tools = tools
        self.connections = connections


class SyncToolArg:
    """Аргументы инструмента снятия по контракту pg_schema_snapshot и его
    собратьев других видов."""

    CONNECTION: ClassVar[str] = "connection"
    SCHEMAS: ClassVar[str] = "schemas"
    BATCH_SIZE: ClassVar[str] = "batch_size"
    PAUSE_MS: ClassVar[str] = "pause_ms"


class SyncJob(BaseModel):
    """Одна запущенная синхронизация: запись, вид источника, инструмент и
    имя подключения для него, контекст вызова с отменой."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    sync: Sync
    source_kind: str
    tool_name: str
    connection_name: str
    context: CallContext

    @property
    def sync_id(self) -> UUID:
        return self.sync.id

    @property
    def cancellation(self) -> RunCancellation:
        return self.context.cancellation

    def call_args(self) -> dict[str, Any]:
        scope = self.sync.scope
        return {
            SyncToolArg.CONNECTION: self.connection_name,
            SyncToolArg.SCHEMAS: scope.schemas_arg(),
            SyncToolArg.BATCH_SIZE: scope.batch_size,
            SyncToolArg.PAUSE_MS: scope.pause_ms,
        }


class QueueSink(FrameSink):
    """Приёмник кадров из потока чтения канала: кладёт кадры в очередь цикла
    событий; None закрывает очередь."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._queue: asyncio.Queue[ToolFrame | None] = asyncio.Queue()

    @property
    def queue(self) -> asyncio.Queue[ToolFrame | None]:
        return self._queue

    def take(self, frame: ToolFrame) -> None:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, frame)

    def close(self) -> None:
        self._queue.put_nowait(None)


class FrameConsumer(SyncFrameReceiver[Awaitable[None]]):
    """Приём кадров одной синхронизации в цикле событий: план в запись,
    порции в staging и накопитель, итог — в снимок. Первая ошибка кадра
    останавливает инструмент и запоминается; остальные кадры сливаются."""

    def __init__(
        self,
        store: SourceStore,
        job: SyncJob,
        queue: asyncio.Queue[ToolFrame | None],
        progress: Progress,
    ) -> None:
        self._store = store
        self._job = job
        self._sync = job.sync
        self._queue = queue
        self._progress = progress
        self._assembler: SnapshotAssembler | None = None
        self._done: SyncDone | None = None
        self._error: str = ""

    @property
    def error(self) -> str:
        return self._error

    @property
    def server_version(self) -> str:
        if self._assembler is None:
            return ""

        return self._assembler.plan.server_version

    async def run(self) -> None:
        while True:
            frame = await self._queue.get()
            if frame is None:
                return

            if self._error:
                continue

            try:
                head = frame.header_as(SyncFrameHead)
                await head.route(self, frame.body)
            except (FrameProtocolError, CatalogError, CatalogServiceError) as exc:
                self._error = f"sync {self._sync.id}: frame rejected: {exc}"
                self._job.cancellation.cancel(StopReason.FAILED)

    def snapshot(self) -> SourceSnapshot:
        """Собранный снимок после итога.

        Ошибки:
        SyncFrameError — итог не пришёл или порции не сложились.
        """
        if self._assembler is None:
            msg = f"sync {self._sync.id}: the tool finished without a sync.plan frame"
            raise SyncFrameError(msg)

        if self._done is None:
            msg = f"sync {self._sync.id}: the tool finished without a sync.done frame"
            raise SyncFrameError(msg)

        return self._assembler.finish(self._done)

    async def on_plan(self, plan: SyncPlan, body: bytes) -> None:
        if self._assembler is not None:
            msg = f"sync {self._sync.id}: a second sync.plan frame arrived"
            raise SyncFrameError(msg)

        if plan.source_kind != self._job.source_kind:
            msg = (
                f"sync {self._sync.id}: the tool reports {plan.source_kind!r} "
                f"source, the synced source is {self._job.source_kind!r}"
            )
            raise SyncFrameError(msg)

        self._assembler = SnapshotAssembler(plan, self._store.kinds)
        self._sync = await self._store.plan_sync(self._sync.id, plan)
        await self._progress(self._sync)

    async def on_batch(self, batch: SyncBatch, body: bytes) -> None:
        if self._assembler is None:
            msg = f"sync {self._sync.id}: sync.batch #{batch.seq} came before sync.plan"
            raise SyncFrameError(msg)

        records = self._assembler.take(batch, body)
        self._sync = await self._store.stage_batch(self._sync.id, batch, records)
        await self._progress(self._sync)

    async def on_done(self, done: SyncDone, body: bytes) -> None:
        self._done = done


class SyncRunner:
    """Запуски синхронизаций инстанса: старт задачей, отмена, ожидание."""

    def __init__(
        self, store: SourceStore, ports: SyncPorts, observer: SyncObserver
    ) -> None:
        self._store = store
        self._tools = ports.tools
        self._names = ports.connections
        self._observer = observer
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._cancellations: dict[UUID, RunCancellation] = {}

    async def start(
        self, caller: SyncCaller, source_id: UUID, request: SyncRequest
    ) -> Sync:
        """Запись синхронизации и задача инструмента; возвращает сразу.

        Ошибки:
        SyncSetupError — инструмента или подключения у субъекта нет.
        SyncRunningError — у источника уже идёт синхронизация.
        SyncConnectionNotBoundError — подключение не привязано к источнику.
        """
        source = await self._store.get_source(source_id)
        snapshot_class = self._store.kinds.snapshot_class(source.kind)
        try:
            tool_name = snapshot_class.sync_tool()
        except CatalogError as exc:
            msg = f"sync of source {source.name!r} cannot start: {exc}"
            raise SyncSetupError(msg) from exc

        invoker = await self._tools.invoker(caller.subject)
        try:
            invoker.tool(tool_name)
        except ToolUnavailableError as exc:
            msg = (
                f"sync of source {source.name!r} cannot start for user "
                f"{caller.subject.login!r}: {exc}"
            )
            raise SyncSetupError(msg) from exc

        if not await self._store.is_bound(source_id, request.connection_id):
            raise SyncConnectionNotBoundError(source_id, request.connection_id)

        connection_name = await self._names.name_of(
            caller.subject, request.connection_id
        )
        sync_id = uuid4()
        sync = await self._store.start_sync(
            sync_id, source_id, request, caller.subject.user_id
        )
        cancellation = RunCancellation()
        job = SyncJob(
            sync=sync,
            source_kind=source.kind,
            tool_name=tool_name,
            connection_name=connection_name,
            context=caller.context(sync_id, cancellation),
        )
        drive = SyncDrive(self._store, job, invoker)
        task = asyncio.create_task(self._guarded(drive, caller.subject))
        self._tasks[sync_id] = task
        self._cancellations[sync_id] = cancellation
        task.add_done_callback(lambda _: self._forget(sync_id))
        await self._observer(caller.subject, sync, ChangeAction.CREATED)
        return sync

    async def cancel(self, subject: Subject, sync_id: UUID) -> Sync:
        """Снять идущую синхронизацию и дождаться закрытия записи.

        Ошибки:
        SyncClosedError — синхронизация уже завершена.
        """
        sync = await self._store.get_sync(sync_id)
        if sync.status is not SyncStatus.RUNNING:
            raise SyncClosedError(sync_id, sync.status)

        task = self._tasks.get(sync_id)
        if task is None:
            reason = "cancelled: the sync task is not running in this instance"
            closed = await self._store.close_sync(sync_id, SyncStatus.CANCELLED, reason)
            await self._observer(subject, closed, ChangeAction.UPDATED)
            return closed

        self._cancellations[sync_id].cancel(StopReason.USER_STOP)
        await asyncio.shield(task)
        return await self._store.get_sync(sync_id)

    async def wait(self, sync_id: UUID) -> Sync:
        """Дождаться конца задачи синхронизации этого инстанса."""
        task = self._tasks.get(sync_id)
        if task is not None:
            await asyncio.shield(task)

        return await self._store.get_sync(sync_id)

    def _forget(self, sync_id: UUID) -> None:
        self._tasks.pop(sync_id, None)
        self._cancellations.pop(sync_id, None)

    async def _guarded(self, drive: SyncDrive, subject: Subject) -> None:
        async def progress(sync: Sync) -> None:
            await self._observer(subject, sync, ChangeAction.UPDATED)

        try:
            closed = await drive.run(progress)
        except Exception:
            logger.exception("sync %s: the drive task crashed", drive.sync_id)
            raise

        await self._observer(subject, closed, ChangeAction.UPDATED)


class SyncDrive:
    """Один прогон инструмента снятия: вызов под контекстом и приёмником
    кадров, приём кадров, итог в запись синхронизации."""

    def __init__(self, store: SourceStore, job: SyncJob, invoker: ToolInvoker) -> None:
        self._store = store
        self._job = job
        self._invoker = invoker

    @property
    def sync_id(self) -> UUID:
        return self._job.sync_id

    async def run(self, progress: Progress) -> Sync:
        loop = asyncio.get_running_loop()
        sink = QueueSink(loop)
        consumer = FrameConsumer(self._store, self._job, sink.queue, progress)
        consuming = asyncio.create_task(consumer.run())

        reply: InvokeReply | None = None
        failure = ""
        try:
            reply = await self._invoke(sink)
        except ToolStopped:
            failure = f"sync {self.sync_id}: {self._job.tool_name} was stopped"
        except Exception as exc:
            failure = f"sync {self.sync_id}: {self._job.tool_name} raised: {exc}"
        finally:
            sink.close()
            await consuming

        return await self._close(reply, failure, consumer)

    async def _invoke(self, sink: QueueSink) -> InvokeReply:
        intent = f"catalog sync {self.sync_id}"
        call = ToolInvoker.call(
            self._job.tool_name, self._job.call_args(), intent, CallIdPrefix.API
        )
        with RunRegistry.open(self._job.context), FrameTap.applied(sink):
            return await self._invoker.invoke(call)

    async def _close(
        self, reply: InvokeReply | None, failure: str, consumer: FrameConsumer
    ) -> Sync:
        if self._job.cancellation.reason is StopReason.USER_STOP:
            return await self._failed(SyncStatus.CANCELLED, "cancelled by the user")

        if consumer.error:
            return await self._failed(SyncStatus.FAILED, consumer.error)

        if failure:
            return await self._failed(SyncStatus.FAILED, failure)

        if reply is not None and not reply.ok:
            error = (
                f"sync {self.sync_id}: {self._job.tool_name} failed: {reply.error_text}"
            )
            return await self._failed(SyncStatus.FAILED, error)

        try:
            snapshot = consumer.snapshot()
        except SyncFrameError as exc:
            return await self._failed(SyncStatus.FAILED, str(exc))

        await self._store.commit_sync(self.sync_id, snapshot, consumer.server_version)
        return await self._store.get_sync(self.sync_id)

    async def _failed(self, status: SyncStatus, error: str) -> Sync:
        return await self._store.close_sync(self.sync_id, status, error)
