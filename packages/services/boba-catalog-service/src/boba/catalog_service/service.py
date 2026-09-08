"""Сервис каталога: права субъекта, сценарии над хранилищами, события шины.

Единственная точка входа для JSON API и инструментов LLM: оба зовут одни и
те же методы с Subject. Чтение процессов и снимков подключений открыто ролям
view_roles и edit_roles; правки, черновики, публикация, синхронизация,
upgrade — только edit_roles; ссылка на просмотр, удаление процесса — его
владельцу. Upgrade — отдельная задача, как синхронизация: запуск возвращается
сразу со статусом running, процессы переводятся по очереди, ход и итог идут
событиями шины; процесс переводится, если совместим с новыми версиями,
иначе итог blocked с проблемами.
Гость по ссылке читает опубликованный процесс без прав. После каждой правки
в область пользователя уходит CatalogChanged.

Ошибки:
CatalogRefusalError — у субъекта нет прав на действие; upgrade процесса
    без версий или поверх идущего запуска.
UpgradeNotFoundError, UpgradeClosedError — как у ProcessStore.
CatalogStoreError, ProcessNotFoundError, ProcessNameTakenError,
    DraftNotFoundError, DraftClosedError, DraftConflictError, DraftStaleError,
    ShareNotFoundError — как у ProcessStore.
SharedNodeNotFoundError — по ссылке запрошен узел, которого нет в процессе.
CatalogOpError — порция операций не применима к снимку черновика.
ConnectionNotSyncedError, ConnectionVersionNotFoundError,
    SnapshotKindMismatchError — как у ConnectionStore.
ConnectionInUseError — подключение стоит в узлах, версии забыть нельзя.
ObjectNotFoundError — по адресу нет объекта в версии снимка.
SnapshotRejectedError — сырой снимок не разобран реестром видов.
UnknownSourceKindError — у вида подключения нет снимка в реестре.
SyncNotFoundError, SyncRunningError, SyncClosedError, SyncSetupError — как у
    SyncRunner.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from boba.catalog import (
    CatalogError,
    CatalogSnapshot,
    NodeColumn,
    ObjectCard,
    ObjectRef,
    OperationList,
    PinnedSnapshot,
    SnapshotResolver,
    SourceDiff,
    SourceKindsError,
    SourceSnapshot,
    Staleness,
    TreeNode,
)
from boba.catalog_service.config import CatalogConfig
from boba.catalog_service.connection_store import ConnectionStore
from boba.catalog_service.process_store import ProcessStore
from boba.catalog_service.records import (
    AuthorVia,
    CatalogAccess,
    CatalogRefusalError,
    CatalogRefusalKind,
    CatalogStoreError,
    ConnectionHasVersionsError,
    ConnectionInUseError,
    ConnectionNotSyncedError,
    ConnectionVersion,
    Draft,
    DraftAuthor,
    DraftState,
    DraftStatus,
    NodeUsage,
    ObjectNotFoundError,
    Process,
    ProcessContext,
    ProcessSpec,
    RebaseResult,
    Share,
    SharedNodeNotFoundError,
    SharedProcess,
    SnapshotKindMismatchError,
    SnapshotRejectedError,
    Sync,
    SyncedConnection,
    SyncScope,
    SyncStatus,
    Upgrade,
    UpgradeReport,
    UpgradeRun,
    UpgradeScope,
    UpgradeStatus,
    UpgradeTarget,
    Version,
    VersionOrigin,
)
from boba.catalog_service.sync_runner import (
    JobTasks,
    SyncCaller,
    SyncPorts,
    SyncRunner,
)
from boba.identity.context import Scope, Subject
from boba.identity.locks import LockToken
from boba.messaging import CatalogChanged, ChangeAction, MessageBus

logger = logging.getLogger(__name__)

__all__ = ["CatalogService"]


class CatalogService:
    """Сценарии каталога от имени субъекта поверх ProcessStore,
    ConnectionStore и шины."""

    def __init__(
        self,
        processes: ProcessStore,
        connections: ConnectionStore,
        cfg: CatalogConfig,
        bus: MessageBus,
        ports: SyncPorts,
    ) -> None:
        self._processes = processes
        self._connections = connections
        self._cfg = cfg
        self._bus = bus
        self._directory = ports.connections
        self._syncs = SyncRunner(connections, ports, self._sync_changed)
        # запуски upgrade этого инстанса: задача и флаг отмены по id запуска
        self._upgrades: JobTasks[asyncio.Event] = JobTasks(asyncio.Event.set)

    @property
    def syncs(self) -> SyncRunner:
        return self._syncs

    @property
    def processes(self) -> ProcessStore:
        return self._processes

    @property
    def connections(self) -> ConnectionStore:
        return self._connections

    @property
    def bus(self) -> MessageBus:
        return self._bus

    def can_view(self, subject: Subject) -> bool:
        """Весь каталог на чтение: роль из view_roles или edit_roles."""
        if self.can_edit(subject):
            return True

        return bool(subject.roles.intersection(self._cfg.view_roles))

    def can_edit(self, subject: Subject) -> bool:
        return bool(subject.roles.intersection(self._cfg.edit_roles))

    def access(self, subject: Subject) -> CatalogAccess:
        return CatalogAccess(
            user_id=subject.user_id,
            login=subject.login,
            can_view=self.can_view(subject),
            can_edit=self.can_edit(subject),
        )

    # --- процессы ---

    async def list_processes(self, subject: Subject) -> Sequence[Process]:
        """Процессы с пометкой отставания привязок от последних версий
        снимков и числом проблем последнего blocked-upgrade."""
        self._require_view(subject)

        latest = await self._latest_pins()
        processes: list[Process] = []
        for process in await self._processes.list_processes():
            processes.append(self._marked_process(process, latest))

        return processes

    async def process(self, subject: Subject, process_id: UUID) -> Process:
        self._require_view(subject)

        process = await self._processes.get_process(process_id)
        return self._marked_process(process, await self._latest_pins())

    @staticmethod
    def _behind(
        pins: Mapping[UUID, int],
        connections: Iterable[UUID],
        latest: Mapping[UUID, int],
    ) -> bool:
        for connection_id in connections:
            pinned = pins.get(connection_id)
            newest = latest.get(connection_id)
            if pinned is None or newest is None:
                continue

            if pinned < newest:
                return True

        return False

    def _marked_process(self, process: Process, latest: Mapping[UUID, int]) -> Process:
        behind = self._behind(process.pins, process.connections, latest)
        attention = process.attention
        if not behind:
            attention = 0

        return process.model_copy(update={"behind": behind, "attention": attention})

    async def create_process(self, subject: Subject, spec: ProcessSpec) -> Process:
        self._require_edit(subject)

        process = await self._processes.create_process(spec, subject.user_id)
        await self._process_changed(subject, process.id, ChangeAction.CREATED)

        return process

    async def update_process(
        self, subject: Subject, process_id: UUID, spec: ProcessSpec
    ) -> Process:
        self._require_edit(subject)

        process = await self._processes.update_process(process_id, spec)
        await self._process_changed(subject, process_id, ChangeAction.UPDATED)

        return process

    async def delete_process(self, subject: Subject, process_id: UUID) -> bool:
        """Процесс со всеми версиями, черновиками и ссылками; только владелец."""
        await self._owned_process(subject, process_id)

        deleted = await self._processes.delete_process(process_id)
        if not deleted:
            return False

        await self._process_changed(subject, process_id, ChangeAction.DELETED)

        return True

    async def snapshot(self, subject: Subject, process_id: UUID) -> CatalogSnapshot:
        self._require_view(subject)

        return await self._processes.snapshot(process_id)

    async def versions(self, subject: Subject, process_id: UUID) -> Sequence[Version]:
        self._require_view(subject)

        return await self._processes.versions(process_id)

    # --- черновики ---

    async def create_draft(
        self, subject: Subject, process_id: UUID | None, name: str
    ) -> Draft:
        """Черновик над текущей версией процесса, привязанный к последним
        версиям всех снимков на момент создания; без процесса — черновик
        нового процесса."""
        self._require_edit(subject)

        pins = await self._latest_pins()
        draft = await self._processes.create_draft(
            process_id, name, subject.user_id, pins
        )
        await self._draft_changed(subject, draft.id, ChangeAction.CREATED)

        return draft

    async def my_drafts(self, subject: Subject) -> Sequence[Draft]:
        """Открытые черновики автора по всем процессам: плоский список панели,
        с пометкой отставания привязок и проблемами последнего upgrade."""
        self._require_view(subject)

        drafts = await self._processes.drafts_of_author(subject.user_id)
        return await self._marked_drafts(drafts)

    async def _marked_drafts(self, drafts: Sequence[Draft]) -> Sequence[Draft]:
        latest = await self._latest_pins()
        marked: list[Draft] = []
        for draft in drafts:
            marked.append(await self._marked_draft(draft, latest))

        return marked

    async def _marked_draft(self, draft: Draft, latest: Mapping[UUID, int]) -> Draft:
        if draft.status is not DraftStatus.OPEN:
            return draft

        state = await self._processes.draft_state(draft.id)
        behind = self._behind(draft.pins, state.snapshot.connections(), latest)
        attention = 0
        if behind:
            last = await self._processes.last_upgrade_of_draft(draft.id)
            if last is not None and last.status is UpgradeStatus.BLOCKED:
                attention = len(last.problems)

        return draft.model_copy(update={"behind": behind, "attention": attention})

    async def rename_draft(self, subject: Subject, draft_id: UUID, name: str) -> Draft:
        self._require_edit(subject)

        draft = await self._processes.rename_draft(draft_id, name)
        await self._draft_changed(subject, draft_id, ChangeAction.UPDATED)

        return draft

    async def draft(self, subject: Subject, draft_id: UUID) -> Draft:
        self._require_view(subject)

        draft = await self._processes.get_draft(draft_id)
        return await self._marked_draft(draft, await self._latest_pins())

    async def draft_state(self, subject: Subject, draft_id: UUID) -> DraftState:
        """Состояние черновика; сам черновик — с пометкой отставания."""
        self._require_view(subject)

        state = await self._processes.draft_state(draft_id)
        marked = await self._marked_draft(state.draft, await self._latest_pins())
        return state.model_copy(update={"draft": marked})

    async def append_ops(
        self,
        subject: Subject,
        draft_id: UUID,
        expected_seq: int,
        ops: OperationList,
        via: AuthorVia,
    ) -> DraftState:
        """Порция операций в черновик; ответ — состояние черновика после неё."""
        self._require_edit(subject)

        author = DraftAuthor(user_id=subject.user_id, via=via)
        draft = await self._processes.get_draft(draft_id)
        resolver = await self._resolver_of(draft.pins)
        await self._processes.append_ops(draft_id, expected_seq, author, ops, resolver)
        await self._draft_changed(subject, draft_id, ChangeAction.UPDATED)

        return await self._processes.draft_state(draft_id)

    async def publish(
        self, subject: Subject, draft_id: UUID, via: AuthorVia
    ) -> Version:
        self._require_edit(subject)

        author = DraftAuthor(user_id=subject.user_id, via=via)
        version = await self._processes.publish(draft_id, author)
        await self._draft_changed(subject, draft_id, ChangeAction.DELETED)
        await self._changed(
            subject,
            CatalogChanged(
                process_id=version.process_id,
                version=version.number,
                action=ChangeAction.CREATED,
            ),
        )

        return version

    async def rebase(
        self, subject: Subject, draft_id: UUID, *, drop_conflicts: bool
    ) -> RebaseResult:
        self._require_edit(subject)

        draft = await self._processes.get_draft(draft_id)
        resolver = await self._resolver_of(draft.pins)
        result = await self._processes.rebase(
            draft_id, drop_conflicts=drop_conflicts, resolver=resolver
        )
        if result.issues and not drop_conflicts:
            return result

        await self._draft_changed(subject, draft_id, ChangeAction.UPDATED)

        return result

    async def discard_draft(self, subject: Subject, draft_id: UUID) -> Draft:
        self._require_edit(subject)

        draft = await self._processes.discard_draft(draft_id)
        await self._draft_changed(subject, draft_id, ChangeAction.DELETED)

        return draft

    # --- upgrade: перевод на последние версии снимков ---

    async def start_upgrade(
        self,
        subject: Subject,
        target: UpgradeTarget,
        entity_id: UUID | None,
        via: AuthorVia,
    ) -> UpgradeRun:
        """Запуск upgrade задачей, как синхронизация: запись со статусом
        running возвращается сразу, процессы переводятся по очереди в задаче
        цикла событий, ход и итог уходят событиями шины по upgrade_id.

        Ошибки:
        ProcessNotFoundError, DraftNotFoundError — цели нет.
        CatalogRefusalError — у процесса нет версий; уже идёт запуск по этой
            цели или по всем.
        """
        self._require_edit(subject)

        targets = await self._upgrade_targets(subject, target, entity_id)
        await self._require_no_running_upgrade(target, entity_id)
        run = await self._processes.start_upgrade_run(
            UpgradeRun(
                id=uuid4(),
                target=target,
                process_id=self._target_process(target, entity_id),
                draft_id=self._target_draft(target, entity_id),
                started_by=subject.user_id,
                started_at=datetime.now(UTC),
                status=SyncStatus.RUNNING,
                total=len(targets),
            )
        )
        cancellation = asyncio.Event()
        work = self._drive_upgrade(subject, run, targets, via, cancellation)
        self._upgrades.start(run.id, work, cancellation)
        await self._upgrade_changed(subject, run.id, ChangeAction.CREATED)

        return run

    async def wait_upgrade(self, run_id: UUID) -> UpgradeRun:
        """Дождаться конца задачи upgrade этого инстанса."""
        await self._upgrades.wait(run_id)

        return await self._processes.get_upgrade_run(run_id)

    async def cancel_upgrade(self, subject: Subject, run_id: UUID) -> UpgradeRun:
        """Снять идущий запуск: текущий процесс дорабатывается, следующие не
        начинаются.

        Ошибки:
        UpgradeNotFoundError — запуска нет.
        UpgradeClosedError — запуск уже завершён.
        """
        self._require_edit(subject)

        stopped = await self._upgrades.cancel(run_id)
        if not stopped:
            reason = "cancelled: the upgrade task is not running in this instance"
            closed = await self._processes.close_upgrade_run(
                run_id, SyncStatus.CANCELLED, reason
            )
            await self._upgrade_changed(subject, run_id, ChangeAction.UPDATED)
            return closed

        return await self._processes.get_upgrade_run(run_id)

    async def upgrade_run(self, subject: Subject, run_id: UUID) -> UpgradeRun:
        self._require_view(subject)

        return await self._processes.get_upgrade_run(run_id)

    async def upgrade_report(self, subject: Subject, run_id: UUID) -> UpgradeReport:
        """Запуск с результатами по процессам."""
        self._require_view(subject)

        run = await self._processes.get_upgrade_run(run_id)
        upgrades = await self._processes.upgrades_of_run(run_id)
        return UpgradeReport(run=run, upgrades=tuple(upgrades))

    async def upgrade_runs(
        self,
        subject: Subject,
        process_id: UUID | None,
        draft_id: UUID | None,
        limit: int,
    ) -> Sequence[UpgradeRun]:
        """Последние запуски, касающиеся процесса или черновика (свои и по
        всем процессам); без фильтра — все последние."""
        self._require_view(subject)

        return await self._processes.upgrade_runs(process_id, draft_id, limit)

    async def last_upgrade(self, subject: Subject, process_id: UUID) -> Upgrade | None:
        self._require_view(subject)

        return await self._processes.last_upgrade_of_process(process_id)

    async def last_draft_upgrade(
        self, subject: Subject, draft_id: UUID
    ) -> Upgrade | None:
        self._require_view(subject)

        return await self._processes.last_upgrade_of_draft(draft_id)

    @staticmethod
    def _target_process(target: UpgradeTarget, entity_id: UUID | None) -> UUID | None:
        if target is UpgradeTarget.PROCESS:
            return entity_id

        return None

    @staticmethod
    def _target_draft(target: UpgradeTarget, entity_id: UUID | None) -> UUID | None:
        if target is UpgradeTarget.DRAFT:
            return entity_id

        return None

    async def _upgrade_targets(
        self, subject: Subject, target: UpgradeTarget, entity_id: UUID | None
    ) -> tuple[UpgradeScope, ...]:
        """Что переводить: отставшие цели с их привязками; процесс или
        черновик без отставания — в списке, итог у него будет moved без версии.

        Ошибки:
        ProcessNotFoundError, DraftNotFoundError — цели нет.
        CatalogRefusalError — у процесса нет версий.
        """
        latest = await self._latest_pins()
        if target is UpgradeTarget.ALL:
            scopes: list[UpgradeScope] = []
            for process in await self.list_processes(subject):
                if not process.behind:
                    continue

                scopes.append(self._process_scope(process, latest))

            return tuple(scopes)

        if entity_id is None:
            msg = f"upgrade of a {target.value} needs its id"
            raise CatalogRefusalError(CatalogRefusalKind.NOT_ALLOWED, msg)

        if target is UpgradeTarget.PROCESS:
            process = await self.process(subject, entity_id)
            if process.latest_version == 0:
                msg = (
                    f"process {process.name!r} ({entity_id}) has no published "
                    "versions: nothing to upgrade"
                )
                raise CatalogRefusalError(CatalogRefusalKind.NOT_ALLOWED, msg)

            return (self._process_scope(process, latest),)

        draft = await self.draft(subject, entity_id)
        return (
            UpgradeScope(
                process_id=draft.process_id,
                draft_id=draft.id,
                pins=draft.pins,
                target=latest,
            ),
        )

    @staticmethod
    def _process_scope(process: Process, latest: Mapping[UUID, int]) -> UpgradeScope:
        return UpgradeScope(
            process_id=process.id, draft_id=None, pins=process.pins, target=latest
        )

    async def _require_no_running_upgrade(
        self, target: UpgradeTarget, entity_id: UUID | None
    ) -> None:
        """Ошибки:
        CatalogRefusalError — по этой цели (или по всем) запуск уже идёт.
        """
        process_id = self._target_process(target, entity_id)
        draft_id = self._target_draft(target, entity_id)
        for run in await self._processes.upgrade_runs(process_id, draft_id, 5):
            if run.status is not SyncStatus.RUNNING:
                continue

            msg = f"upgrade {run.id} ({run.target.value}) is still running"
            raise CatalogRefusalError(CatalogRefusalKind.NOT_ALLOWED, msg)

    async def _drive_upgrade(
        self,
        subject: Subject,
        run: UpgradeRun,
        targets: Sequence[UpgradeScope],
        via: AuthorVia,
        cancellation: asyncio.Event,
    ) -> None:
        """Задача запуска: цели по очереди, ход — событием после каждой;
        сбой одной цели — её итог blocked с причиной, остальные идут."""
        author = DraftAuthor(user_id=subject.user_id, via=via)
        status = SyncStatus.DONE
        error: str | None = None
        try:
            for scope in targets:
                if cancellation.is_set():
                    status = SyncStatus.CANCELLED
                    error = "cancelled by the user"
                    break

                upgrade = await self._upgrade_scope(subject, run.id, scope, author)
                await self._processes.advance_upgrade_run(run.id, upgrade)
                await self._upgrade_changed(subject, run.id, ChangeAction.UPDATED)
        except Exception as exc:
            logger.exception("catalog: upgrade run %s crashed", run.id)
            status = SyncStatus.FAILED
            error = f"upgrade {run.id} crashed: {exc}"

        await self._processes.close_upgrade_run(run.id, status, error)
        await self._upgrade_changed(subject, run.id, ChangeAction.UPDATED)

    async def _upgrade_scope(
        self, subject: Subject, run_id: UUID, scope: UpgradeScope, author: DraftAuthor
    ) -> Upgrade:
        """Одна цель запуска; сбой сервиса на ней — итог blocked с причиной."""
        try:
            if scope.draft_id is not None:
                return await self._upgrade_draft(subject, run_id, scope, author)

            return await self._upgrade_process(subject, run_id, scope, author)
        except CatalogError as exc:
            logger.warning("catalog: upgrade of %s failed: %s", scope, exc)
            failed = scope.unchanged(run_id, author, UpgradeStatus.BLOCKED)
            return await self._processes.record_upgrade(failed)

    async def _upgrade_process(
        self, subject: Subject, run_id: UUID, scope: UpgradeScope, author: DraftAuthor
    ) -> Upgrade:
        """Процесс совместим — новая версия с новыми привязками, иначе запись
        blocked; без отставания — moved без версии и без записи."""
        process_id = scope.process_id
        if process_id is None:
            msg = "upgrade scope of a process without process_id"
            raise CatalogStoreError(msg)

        process = await self.process(subject, process_id)
        if not process.behind:
            return self._untouched(run_id, scope, author)

        current = self._process_scope(process, scope.target)
        snapshot = await self._processes.snapshot(process_id)
        problems = await self._staleness_of(snapshot, process.pins)
        if problems.entries:
            upgrade = await self._blocked(run_id, current, problems, author)
            await self._process_changed(subject, process_id, ChangeAction.UPDATED)
            return upgrade

        version = await self._processes.publish_pins(process_id, scope.target, author)
        moved = current.moved(
            run_id, author, scope.target, version.published_at, version.number
        )
        upgrade = await self._processes.record_upgrade(moved)
        await self._changed(
            subject,
            CatalogChanged(
                process_id=process_id,
                version=version.number,
                action=ChangeAction.CREATED,
            ),
        )

        return upgrade

    async def _upgrade_draft(
        self, subject: Subject, run_id: UUID, scope: UpgradeScope, author: DraftAuthor
    ) -> Upgrade:
        """Черновик совместим — новые привязки, иначе запись blocked; без
        отставания — moved без записи."""
        draft_id = scope.draft_id
        if draft_id is None:
            msg = "upgrade scope of a draft without draft_id"
            raise CatalogStoreError(msg)

        state = await self._processes.draft_state(draft_id)
        if not self._behind(scope.pins, state.snapshot.connections(), scope.target):
            return self._untouched(run_id, scope, author)

        problems = await self._staleness_of(state.snapshot, scope.pins)
        if problems.entries:
            upgrade = await self._blocked(run_id, scope, problems, author)
            await self._draft_changed(subject, draft_id, ChangeAction.UPDATED)
            return upgrade

        moved = await self._processes.set_pins(draft_id, scope.target)
        outcome = scope.moved(run_id, author, moved.pins, datetime.now(UTC))
        upgrade = await self._processes.record_upgrade(outcome)
        await self._draft_changed(subject, draft_id, ChangeAction.UPDATED)

        return upgrade

    @staticmethod
    def _untouched(run_id: UUID, scope: UpgradeScope, author: DraftAuthor) -> Upgrade:
        return scope.unchanged(run_id, author, UpgradeStatus.MOVED)

    async def _blocked(
        self,
        run_id: UUID,
        scope: UpgradeScope,
        problems: Staleness,
        author: DraftAuthor,
    ) -> Upgrade:
        blocked = scope.blocked(run_id, author, problems.entries)
        return await self._processes.record_upgrade(blocked)

    async def _draft_changed(
        self, subject: Subject, draft_id: UUID, action: ChangeAction
    ) -> None:
        await self._changed(subject, CatalogChanged(draft_id=draft_id, action=action))

    async def _upgrade_changed(
        self, subject: Subject, run_id: UUID, action: ChangeAction
    ) -> None:
        await self._changed(subject, CatalogChanged(upgrade_id=run_id, action=action))

    # --- контекст и устаревание ---

    async def published_pins(
        self, subject: Subject, process_id: UUID
    ) -> Mapping[UUID, int]:
        """Привязки последней версии процесса; без версий — пусто."""
        self._require_view(subject)

        return await self._published_pins(process_id)

    async def resolver_of(
        self, subject: Subject, pins: Mapping[UUID, int]
    ) -> SnapshotResolver:
        """Резолвер объектов по привязанным версиям снимков."""
        self._require_view(subject)

        return await self._resolver_of(pins)

    async def staleness(self, subject: Subject, process_id: UUID) -> Staleness:
        """Устаревание опубликованного процесса относительно последних версий
        снимков, по привязкам последней версии процесса."""
        self._require_view(subject)

        snapshot = await self._processes.snapshot(process_id)
        pins = await self._published_pins(process_id)
        return await self._staleness_of(snapshot, pins)

    async def draft_staleness(self, subject: Subject, draft_id: UUID) -> Staleness:
        self._require_view(subject)

        state = await self._processes.draft_state(draft_id)
        return await self._staleness_of(state.snapshot, state.draft.pins)

    async def context(self, subject: Subject, process_id: UUID) -> ProcessContext:
        """Контекст опубликованного процесса по привязкам последней версии."""
        self._require_view(subject)

        return await self._published_context(process_id)

    async def draft_context(self, subject: Subject, draft_id: UUID) -> ProcessContext:
        self._require_view(subject)

        state = await self._processes.draft_state(draft_id)
        return await self._context_of(state.snapshot, state.draft.pins)

    async def _published_pins(self, process_id: UUID) -> Mapping[UUID, int]:
        versions = await self._processes.versions(process_id)
        if not versions:
            return {}

        return versions[-1].pins

    async def _published_context(self, process_id: UUID) -> ProcessContext:
        snapshot = await self._processes.snapshot(process_id)
        pins = await self._published_pins(process_id)
        return await self._context_of(snapshot, pins)

    async def _latest_pins(self) -> dict[UUID, int]:
        pins: dict[UUID, int] = {}
        for synced in await self._connections.synced_connections():
            pins[synced.connection_id] = synced.latest_version

        return pins

    async def _resolver_of(self, pins: Mapping[UUID, int]) -> SnapshotResolver:
        """Снимки привязанных версий; привязка к подключению, версии которого
        забыты, пропускается — его объекты резолвер считает существующими."""
        snapshots: dict[UUID, SourceSnapshot] = {}
        for connection_id, version in pins.items():
            try:
                snapshots[connection_id] = await self._connections.snapshot_of(
                    connection_id, version
                )
            except ConnectionNotSyncedError:
                continue

        return SnapshotResolver(snapshots)

    async def _context_of(
        self, snapshot: CatalogSnapshot, pins: Mapping[UUID, int]
    ) -> ProcessContext:
        resolver = await self._resolver_of(pins)

        columns: dict[UUID, tuple[NodeColumn, ...]] = {}
        for node in snapshot.nodes.values():
            columns[node.id] = resolver.node_columns(node.ref)

        stale = await self._staleness_of(snapshot, pins)
        return ProcessContext(pins=pins, columns=columns, stale=stale)

    async def _staleness_of(
        self, snapshot: CatalogSnapshot, pins: Mapping[UUID, int]
    ) -> Staleness:
        pinned: dict[UUID, PinnedSnapshot] = {}
        latest: dict[UUID, PinnedSnapshot] = {}
        for connection_id in snapshot.connections():
            pinned_version = pins.get(connection_id)
            if pinned_version is None:
                continue

            synced = await self._connections.synced_or_none(connection_id)
            if synced is None:
                continue

            if synced.latest_version == pinned_version:
                continue

            pinned[connection_id] = PinnedSnapshot(
                version=pinned_version,
                snapshot=await self._connections.snapshot_of(
                    connection_id, pinned_version
                ),
            )
            latest[connection_id] = PinnedSnapshot(
                version=synced.latest_version,
                snapshot=await self._connections.latest_snapshot(connection_id),
            )

        return Staleness.compute(snapshot, pinned, latest)

    # --- ссылки на просмотр ---

    async def share_process(self, subject: Subject, process_id: UUID) -> Share:
        """Новая ссылка на просмотр; только владелец процесса."""
        await self._owned_process(subject, process_id)

        share = await self._processes.create_share(process_id, subject.user_id)
        await self._process_changed(subject, process_id, ChangeAction.UPDATED)

        return share

    async def shares(self, subject: Subject, process_id: UUID) -> Sequence[Share]:
        await self._owned_process(subject, process_id)

        return await self._processes.shares_of(process_id)

    async def revoke_share(self, subject: Subject, token: str) -> Share:
        share = await self._processes.get_share(token)
        await self._owned_process(subject, share.process_id)

        revoked = await self._processes.revoke_share(token)
        await self._process_changed(subject, share.process_id, ChangeAction.UPDATED)

        return revoked

    async def shared_process(self, token: str) -> SharedProcess:
        """Опубликованный процесс по действующей ссылке: прав не нужно.

        Ошибки:
        ShareNotFoundError — ссылки нет или она отозвана.
        """
        share = await self._processes.get_share(token)
        process = await self._processes.get_process(share.process_id)
        snapshot = await self._processes.snapshot(share.process_id)
        context = await self._published_context(share.process_id)
        return SharedProcess(process=process, snapshot=snapshot, context=context)

    async def shared_object(self, token: str, node_id: UUID) -> ObjectCard:
        """Карточка объекта узла по ссылке, из версии, привязанной публикацией.

        Ошибки:
        ShareNotFoundError — ссылки нет или она отозвана.
        SharedNodeNotFoundError — узла нет в опубликованном процессе.
        ObjectNotFoundError — объекта нет в привязанной версии.
        """
        share = await self._processes.get_share(token)
        snapshot = await self._processes.snapshot(share.process_id)
        node = snapshot.nodes.get(node_id)
        if node is None:
            raise SharedNodeNotFoundError(token, node_id)

        pins = await self._published_pins(share.process_id)
        version = pins.get(node.ref.connection_id, -1)
        return await self._card(node.ref, version)

    # --- подключения глазами каталога ---

    async def synced_connections(self, subject: Subject) -> Sequence[SyncedConnection]:
        """Подключения с версиями снимка по последней версии каждого."""
        self._require_view(subject)

        return await self._connections.synced_connections()

    async def synced_connection(
        self, subject: Subject, connection_id: UUID
    ) -> SyncedConnection:
        self._require_view(subject)

        return await self._connections.synced(connection_id)

    def source_kinds(self) -> tuple[str, ...]:
        """Виды подключений, у которых установлен снимок: kind типов соединений."""
        return self._connections.kinds.kinds()

    async def connection_versions(
        self, subject: Subject, connection_id: UUID
    ) -> Sequence[ConnectionVersion]:
        self._require_view(subject)

        return await self._connections.versions_of(connection_id)

    async def connection_snapshot(
        self, subject: Subject, connection_id: UUID, version: int
    ) -> SourceSnapshot:
        """Снимок версии; version 0 — пустой снимок, отрицательная — последняя."""
        self._require_view(subject)

        if version < 0:
            return await self._connections.latest_snapshot(connection_id)

        return await self._connections.snapshot_of(connection_id, version)

    async def connection_tree(
        self,
        subject: Subject,
        connection_id: UUID,
        version: int,
        path: Sequence[str],
    ) -> Sequence[TreeNode]:
        """Дети узла дерева снимка версии: из хранилища читаются только записи
        области этого пути, снимок целиком не поднимается."""
        self._require_view(subject)

        resolved = await self._resolve_version(connection_id, version)
        synced = await self._connections.synced(connection_id)
        scope = self._connections.kinds.snapshot_class(synced.kind).tree_scope(path)
        snapshot = await self._connections.tree_snapshot(connection_id, resolved, scope)
        return snapshot.children(connection_id, path)

    async def connection_object(
        self, subject: Subject, ref: ObjectRef, version: int
    ) -> ObjectCard:
        """Карточка объекта по адресу в версии снимка (отрицательная — последняя).

        Ошибки:
        ObjectNotFoundError — по адресу нет объекта.
        """
        self._require_view(subject)

        return await self._card(ref, version)

    async def connection_diff(
        self, subject: Subject, connection_id: UUID, old: int, new: int
    ) -> SourceDiff:
        self._require_view(subject)

        return await self._connections.diff_of(connection_id, old, new)

    def parse_snapshot(self, raw: Mapping[str, Any]) -> SourceSnapshot:
        """Снимок из JSON по полю kind: граница api и инструментов.

        Ошибки:
        SnapshotRejectedError — kind неизвестен или тело не по модели.
        """
        try:
            return self._connections.kinds.parse(raw)
        except SourceKindsError as exc:
            raise SnapshotRejectedError(str(exc)) from exc

    async def write_connection_version(
        self, subject: Subject, connection_id: UUID, snapshot: SourceSnapshot
    ) -> ConnectionVersion:
        """Версия целиком от имени субъекта: путь стенда. Имя подключения —
        из справочника глазами субъекта.

        Ошибки:
        SyncSetupError — подключение субъекту не видно.
        UnknownSourceKindError — у вида подключения нет снимка.
        SnapshotKindMismatchError — снимок не того вида, что подключение.
        """
        self._require_edit(subject)

        connection = await self._directory.info_of(subject, connection_id)
        self._connections.snapshot_class(connection.kind)
        if snapshot.kind != connection.kind:
            raise SnapshotKindMismatchError(
                connection_id, connection.kind, snapshot.kind
            )

        origin = VersionOrigin(
            taken_by=subject.user_id, connection_name=connection.name
        )
        version = await self._connections.write_version(connection_id, snapshot, origin)
        await self._connection_changed(subject, connection_id, ChangeAction.UPDATED)

        return version

    async def forget_versions(self, subject: Subject, connection_id: UUID) -> int:
        """Все версии снимка подключения; отказ, пока подключение стоит в узлах.

        Ошибки:
        ConnectionInUseError — узлы процессов или открытых черновиков.
        SyncRunningError — синхронизация ещё пишет.
        """
        self._require_edit(subject)

        usage = await self._usage_of(connection_id)
        if usage:
            name = await self._stored_name(connection_id)
            raise ConnectionInUseError(connection_id, name, usage)

        forgotten = await self._connections.forget_versions(connection_id)
        if forgotten == 0:
            return 0

        await self._connection_changed(subject, connection_id, ChangeAction.DELETED)

        return forgotten

    async def holding_reason(self, connection_id: UUID) -> str:
        """Почему подключение нельзя удалить: узлы процессов или версии
        снимка; пусто — каталог его не держит. Для DeleteGuard брокера."""
        usage = await self._usage_of(connection_id)
        if usage:
            name = await self._stored_name(connection_id)
            return str(ConnectionInUseError(connection_id, name, usage))

        synced = await self._connections.synced_or_none(connection_id)
        if synced is None:
            return ""

        return str(
            ConnectionHasVersionsError(
                connection_id, synced.name, synced.latest_version
            )
        )

    async def _usage_of(self, connection_id: UUID) -> Sequence[NodeUsage]:
        """Узлы над подключением: опубликованные и в открытых черновиках."""
        usage = list(await self._processes.usage_of_connection(connection_id))
        for draft in await self._processes.open_drafts():
            state = await self._processes.draft_state(draft.id)
            count = 0
            for node in state.snapshot.nodes.values():
                if node.ref.connection_id != connection_id:
                    continue

                count += 1

            if count == 0:
                continue

            process_name = draft.name
            if draft.process_id is not None:
                process = await self._processes.get_process(draft.process_id)
                process_name = process.name

            usage.append(
                NodeUsage(
                    process_id=draft.process_id,
                    process_name=process_name,
                    draft=draft.name,
                    nodes=count,
                )
            )

        return usage

    async def _stored_name(self, connection_id: UUID) -> str:
        synced = await self._connections.synced_or_none(connection_id)
        if synced is None:
            return str(connection_id)

        return synced.name

    async def _card(self, ref: ObjectRef, version: int) -> ObjectCard:
        resolved = await self._resolve_version(ref.connection_id, version)
        snapshot = await self._connections.snapshot_of(ref.connection_id, resolved)
        try:
            return snapshot.card(ref)
        except CatalogError as exc:
            where = f"connection {ref.connection_id} version {resolved}"
            raise ObjectNotFoundError(ref, where, str(exc)) from exc

    async def _resolve_version(self, connection_id: UUID, version: int) -> int:
        if version >= 0:
            return version

        synced = await self._connections.synced(connection_id)
        return synced.latest_version

    # --- синхронизации ---

    async def start_sync(
        self, caller: SyncCaller, connection_id: UUID, scope: SyncScope
    ) -> Sync:
        """Синхронизация подключения инструментом вида от имени субъекта:
        нужны edit_roles, видимое подключение и доступ к инструменту."""
        self._require_edit(caller.subject)

        return await self._syncs.start(caller, connection_id, scope)

    async def cancel_sync(self, subject: Subject, sync_id: UUID) -> Sync:
        self._require_edit(subject)

        return await self._syncs.cancel(subject, sync_id)

    async def sync(self, subject: Subject, sync_id: UUID) -> Sync:
        self._require_view(subject)

        return await self._connections.get_sync(sync_id)

    async def connection_syncs(
        self, subject: Subject, connection_id: UUID
    ) -> Sequence[Sync]:
        self._require_view(subject)

        return await self._connections.syncs_of(connection_id)

    async def _sync_changed(
        self, subject: Subject, sync: Sync, action: ChangeAction
    ) -> None:
        await self._changed(subject, CatalogChanged(sync_id=sync.id, action=action))
        if action is ChangeAction.CREATED:
            return

        if sync.status is SyncStatus.RUNNING:
            return

        await self._connection_changed(
            subject, sync.connection_id, ChangeAction.UPDATED
        )

    # --- права ---

    def _require_view(self, subject: Subject) -> None:
        if self.can_view(subject):
            return

        allowed = sorted({*self._cfg.view_roles, *self._cfg.edit_roles})
        msg = (
            f"user {subject.login!r} has no role to read the catalog: "
            f"one of {allowed} is required"
        )
        raise CatalogRefusalError(CatalogRefusalKind.VIEW_FORBIDDEN, msg)

    def _require_edit(self, subject: Subject) -> None:
        if self.can_edit(subject):
            return

        allowed = sorted(self._cfg.edit_roles)
        msg = (
            f"user {subject.login!r} has no role to edit the catalog: "
            f"one of {allowed} is required"
        )
        raise CatalogRefusalError(CatalogRefusalKind.EDIT_FORBIDDEN, msg)

    async def _owned_process(self, subject: Subject, process_id: UUID) -> Process:
        """Удалять и шарить процесс может его владелец с правом на правки."""
        self._require_edit(subject)

        process = await self._processes.get_process(process_id)
        if process.owner_id == subject.user_id:
            return process

        msg = (
            f"user {subject.login!r} does not own process {process.name!r} "
            f"({process.id}); only the owner can delete or share it"
        )
        raise CatalogRefusalError(CatalogRefusalKind.NOT_OWNER, msg)

    # --- события ---

    async def _process_changed(
        self, subject: Subject, process_id: UUID, action: ChangeAction
    ) -> None:
        await self._changed(
            subject, CatalogChanged(process_id=process_id, action=action)
        )

    async def _connection_changed(
        self, subject: Subject, connection_id: UUID, action: ChangeAction
    ) -> None:
        await self._changed(
            subject, CatalogChanged(connection_id=connection_id, action=action)
        )

    async def _changed(self, subject: Subject, message: CatalogChanged) -> None:
        await self._bus.publish(Scope.user(subject.user_id), message, LockToken.local())
