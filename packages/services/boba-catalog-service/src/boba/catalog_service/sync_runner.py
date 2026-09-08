"""Синхронизация подключения инструментом снятия: запуск инструмента вида
(SourceSnapshot.SYNC_TOOL) от имени субъекта вне чата и запись версии по его
итогу. Строки снимка инструмент кладёт в домен каталога сам; хост по
SnapshotOutcome из результата записывает шапку версии и закрывает
синхронизацию.

Запуск живёт задачей цикла событий инстанса: SyncRunner держит задачи и
отмены по id синхронизации, cancel() снимает инструмент через RunCancellation
и ждёт закрытия записи. Инструменты и имена подключений приходят портами
SyncTools и ConnectionDirectory, которые собирает хост приложения.

Ошибки:
CatalogStoreError — Postgres недоступен или ответ битый.
SyncNotFoundError — синхронизации с таким id нет.
SyncRunningError — у подключения уже идёт синхронизация.
SyncClosedError — синхронизация уже завершена, отменять нечего.
SnapshotKindMismatchError — прежние версии подключения другого вида.
UnknownSourceKindError — вида подключения нет в реестре снимков.
SyncSetupError — синхронизацию не запустить: у вида нет инструмента снятия,
    инструмент недоступен субъекту, подключение субъекту не видно.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, ClassVar, Generic, Protocol, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from boba.cancellation import RunCancellation, StopReason, ToolStopped
from boba.catalog import CatalogError
from boba.catalog_service.connection_store import ConnectionStore
from boba.catalog_service.records import (
    CatalogServiceError,
    ConnectionInfo,
    SnapshotKindMismatchError,
    Sync,
    SyncOutcomeError,
    SyncRequest,
    SyncScope,
    SyncStatus,
)
from boba.db.postgres.catalog import CatalogDomainError, SnapshotOutcome
from boba.identity.api import ApiSubject
from boba.identity.context import (
    CallContext,
    Credential,
    HumanInitiator,
    Initiator,
    Scope,
    Subject,
)
from boba.identity.run import RunRegistry
from boba.messaging import ChangeAction
from boba.toolkit.calls import CallIdPrefix
from boba.toolkit.failure import ToolUnavailableError
from boba.toolrun.invoke import InvokeReply, ToolInvoker
from boba.toolrun.registry import ToolRegistry

logger = logging.getLogger(__name__)

__all__ = [
    "ConnectionDirectory",
    "JobTasks",
    "RegistrySyncTools",
    "SyncCaller",
    "SyncObserver",
    "SyncPorts",
    "SyncRunner",
    "SyncSetupError",
    "SyncTools",
]

SyncObserver = Callable[[Subject, Sync, ChangeAction], Awaitable[None]]
CancelT = TypeVar("CancelT")


class JobTasks(Generic[CancelT]):
    """Задачи одного инстанса по id: синхронизации и upgrade'ы стартуют
    задачей цикла событий, отменяются ручкой отмены и ждутся под shield.
    Завершённая задача забывается сама; задача другого инстанса здесь
    неизвестна, и вызывающий закрывает её запись в базе сам."""

    def __init__(self, stop: Callable[[CancelT], None]) -> None:
        self._stop = stop
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._cancellations: dict[UUID, CancelT] = {}

    def start(
        self, job_id: UUID, work: Coroutine[Any, Any, None], cancellation: CancelT
    ) -> None:
        task = asyncio.create_task(work)
        self._tasks[job_id] = task
        self._cancellations[job_id] = cancellation
        task.add_done_callback(lambda _: self._forget(job_id))

    async def wait(self, job_id: UUID) -> None:
        """Дождаться конца задачи; задачи нет в этом инстансе — сразу."""
        task = self._tasks.get(job_id)
        if task is None:
            return

        await asyncio.shield(task)

    async def cancel(self, job_id: UUID) -> bool:
        """Остановить задачу и дождаться её; False — задачи в этом инстансе нет."""
        task = self._tasks.get(job_id)
        if task is None:
            return False

        self._stop(self._cancellations[job_id])
        await asyncio.shield(task)

        return True

    def _forget(self, job_id: UUID) -> None:
        self._tasks.pop(job_id, None)
        self._cancellations.pop(job_id, None)


class SyncSetupError(CatalogServiceError):
    """Синхронизацию не запустить: нет инструмента, доступа или подключения."""


class SyncCaller(BaseModel):
    """От чьего имени и откуда запущена синхронизация: субъект, инициатор и
    секреты для инструмента снятия."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    subject: Subject
    initiator: Initiator
    credential: Credential

    @classmethod
    def of_context(cls, context: CallContext) -> SyncCaller:
        """Вызывающий из контекста вызова инструмента."""
        return cls(
            subject=context.subject,
            initiator=context.initiator,
            credential=context.credential,
        )

    @classmethod
    def of_api(cls, identity: ApiSubject) -> SyncCaller:
        """Вызывающий из входа API: инструмент снятия ходит в базу под его билетом."""
        return cls(
            subject=identity.subject,
            initiator=HumanInitiator(via="api"),
            credential=identity.credential,
        )

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


RegistryRef = Callable[[], Awaitable[ToolRegistry]]


class RegistrySyncTools(SyncTools):
    """Реализация SyncTools реестром инструментов процесса: набор вне чата
    по ролям и профилю субъекта; реестр берётся ссылкой на каждый вызов."""

    def __init__(self, registry: RegistryRef) -> None:
        self._registry = registry

    async def invoker(self, subject: Subject) -> ToolInvoker:
        registry = await self._registry()
        return ToolInvoker.for_subject(registry, subject)


class ConnectionDirectory(Protocol):
    """Справочник подключений глазами субъекта: по id и по имени.

    Ошибки:
    SyncSetupError — подключение субъекту не видно или справочник недоступен.
    """

    @abstractmethod
    async def info_of(
        self, subject: Subject, connection_id: UUID
    ) -> ConnectionInfo: ...

    @abstractmethod
    async def named(self, subject: Subject, name: str) -> ConnectionInfo: ...


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


class SyncJob(BaseModel):
    """Одна запущенная синхронизация: запись (с именем и видом подключения),
    инструмент снятия и контекст вызова с отменой."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    sync: Sync
    tool_name: str
    context: CallContext

    @property
    def sync_id(self) -> UUID:
        return self.sync.id

    @property
    def cancellation(self) -> RunCancellation:
        return self.context.cancellation

    def call_args(self) -> dict[str, Any]:
        return {
            SyncToolArg.CONNECTION: self.sync.connection_name,
            SyncToolArg.SCHEMAS: self.sync.scope.schemas_arg(),
        }


class SyncRunner:
    """Запуски синхронизаций инстанса: старт задачей, отмена, ожидание."""

    def __init__(
        self, store: ConnectionStore, ports: SyncPorts, observer: SyncObserver
    ) -> None:
        self._store = store
        self._tools = ports.tools
        self._names = ports.connections
        self._observer = observer
        self._jobs: JobTasks[RunCancellation] = JobTasks(self._stop)

    @property
    def directory(self) -> ConnectionDirectory:
        return self._names

    async def start(
        self, caller: SyncCaller, connection_id: UUID, scope: SyncScope
    ) -> Sync:
        """Запись синхронизации и задача инструмента; возвращает сразу.

        Ошибки:
        SyncSetupError — инструмента или подключения у субъекта нет, у вида
            нет инструмента снятия.
        UnknownSourceKindError — вида подключения нет в реестре снимков.
        SyncRunningError — у подключения уже идёт синхронизация.
        SnapshotKindMismatchError — прежние версии другого вида.
        """
        connection = await self._names.info_of(caller.subject, connection_id)
        snapshot_class = self._store.snapshot_class(connection.kind)
        try:
            tool_name = snapshot_class.sync_tool()
        except CatalogError as exc:
            msg = f"sync of connection {connection.name!r} cannot start: {exc}"
            raise SyncSetupError(msg) from exc

        invoker = await self._tools.invoker(caller.subject)
        try:
            invoker.tool(tool_name)
        except ToolUnavailableError as exc:
            msg = (
                f"sync of connection {connection.name!r} cannot start for user "
                f"{caller.subject.login!r}: {exc}"
            )
            raise SyncSetupError(msg) from exc

        request = SyncRequest(connection=connection, scope=scope)
        sync_id = uuid4()
        sync = await self._store.start_sync(sync_id, request, caller.subject.user_id)
        cancellation = RunCancellation()
        job = SyncJob(
            sync=sync,
            tool_name=tool_name,
            context=caller.context(sync_id, cancellation),
        )
        drive = SyncDrive(self._store, job, invoker)
        self._jobs.start(sync_id, self._guarded(drive, caller.subject), cancellation)
        await self._observer(caller.subject, sync, ChangeAction.CREATED)

        return sync

    async def cancel(self, subject: Subject, sync_id: UUID) -> Sync:
        """Снять идущую синхронизацию и дождаться закрытия записи.

        Ошибки:
        SyncClosedError — синхронизация уже завершена.
        """
        stopped = await self._jobs.cancel(sync_id)
        if not stopped:
            reason = "cancelled: the sync task is not running in this instance"
            closed = await self._store.close_sync(sync_id, SyncStatus.CANCELLED, reason)
            await self._observer(subject, closed, ChangeAction.UPDATED)
            return closed

        return await self._store.get_sync(sync_id)

    async def wait(self, sync_id: UUID) -> Sync:
        """Дождаться конца задачи синхронизации этого инстанса."""
        await self._jobs.wait(sync_id)

        return await self._store.get_sync(sync_id)

    @staticmethod
    def _stop(cancellation: RunCancellation) -> None:
        cancellation.cancel(StopReason.USER_STOP)

    async def _guarded(self, drive: SyncDrive, subject: Subject) -> None:
        try:
            closed = await drive.run()
        except Exception:
            logger.exception("sync %s: the drive task crashed", drive.sync_id)
            raise

        await self._observer(subject, closed, ChangeAction.UPDATED)


class SyncDrive:
    """Один прогон инструмента снятия: вызов под контекстом с отменой, итог
    инструмента — в шапку версии и запись синхронизации."""

    def __init__(
        self, store: ConnectionStore, job: SyncJob, invoker: ToolInvoker
    ) -> None:
        self._store = store
        self._job = job
        self._invoker = invoker

    @property
    def sync_id(self) -> UUID:
        return self._job.sync_id

    async def run(self) -> Sync:
        reply: InvokeReply | None = None
        failure = ""
        try:
            reply = await self._invoke()
        except ToolStopped:
            failure = f"sync {self.sync_id}: {self._job.tool_name} was stopped"
        except Exception as exc:
            failure = f"sync {self.sync_id}: {self._job.tool_name} raised: {exc}"

        return await self._close(reply, failure)

    async def _invoke(self) -> InvokeReply:
        intent = f"catalog sync {self.sync_id}"
        call = ToolInvoker.call(
            self._job.tool_name, self._job.call_args(), intent, CallIdPrefix.API
        )
        with RunRegistry.open(self._job.context):
            return await self._invoker.invoke(call)

    async def _close(self, reply: InvokeReply | None, failure: str) -> Sync:
        if self._job.cancellation.reason is StopReason.USER_STOP:
            return await self._failed(SyncStatus.CANCELLED, "cancelled by the user")

        if failure:
            return await self._failed(SyncStatus.FAILED, failure)

        if reply is None:
            error = f"sync {self.sync_id}: {self._job.tool_name} returned no reply"
            return await self._failed(SyncStatus.FAILED, error)

        if not reply.ok:
            error = (
                f"sync {self.sync_id}: {self._job.tool_name} failed: {reply.error_text}"
            )
            return await self._failed(SyncStatus.FAILED, error)

        try:
            outcome = SnapshotOutcome.of_result(reply.result)
            return await self._store.record_sync(self.sync_id, outcome)
        except (CatalogDomainError, SyncOutcomeError, SnapshotKindMismatchError) as exc:
            return await self._failed(SyncStatus.FAILED, f"sync {self.sync_id}: {exc}")

    async def _failed(self, status: SyncStatus, error: str) -> Sync:
        return await self._store.close_sync(self.sync_id, status, error)
