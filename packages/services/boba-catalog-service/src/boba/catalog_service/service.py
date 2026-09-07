"""Сервис каталога: права субъекта, сценарии над хранилищами, события шины.

Единственная точка входа для JSON API и инструментов LLM: оба зовут одни и
те же методы с Subject. Чтение процессов и снимков подключений открыто ролям
view_roles и edit_roles; правки, черновики, публикация, синхронизация —
только edit_roles; ссылка на просмотр, удаление процесса — его владельцу.
Гость по ссылке читает опубликованный процесс без прав. После каждой правки
в область пользователя уходит CatalogChanged.

Ошибки:
CatalogRefusalError — у субъекта нет прав на действие.
CatalogStoreError, ProcessNotFoundError, ProcessNameTakenError,
    DraftNotFoundError, DraftClosedError, DraftConflictError, DraftStaleError,
    ShareNotFoundError — как у ProcessStore.
SharedNodeNotFoundError — по ссылке запрошен узел, которого нет в процессе.
CatalogOpError — порция операций не применима к снимку черновика.
ConnectionNotSyncedError, ConnectionVersionNotFoundError,
    SnapshotKindMismatchError — как у ConnectionStore.
ConnectionInUseError — подключение стоит в узлах, версии забыть нельзя.
ObjectNotFoundError — по адресу нет объекта в версии снимка.
UnknownSourceKindError — у вида подключения нет снимка в реестре.
SyncNotFoundError, SyncRunningError, SyncClosedError, SyncSetupError — как у
    SyncRunner.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import ClassVar
from uuid import UUID

from boba.catalog import (
    CatalogError,
    CatalogSnapshot,
    ChangeStatus,
    NodeColumn,
    ObjectCard,
    ObjectRef,
    OperationList,
    PinnedSnapshot,
    SnapshotResolver,
    SourceDiff,
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
    PinBump,
    Process,
    ProcessContext,
    ProcessSpec,
    RebaseResult,
    Share,
    SharedNodeNotFoundError,
    SharedProcess,
    SnapshotKindMismatchError,
    Sync,
    SyncedConnection,
    SyncScope,
    SyncStatus,
    UnknownSourceKindError,
    Version,
    VersionOrigin,
)
from boba.catalog_service.sync_runner import SyncCaller, SyncPorts, SyncRunner
from boba.identity.context import Scope, Subject
from boba.identity.locks import LockToken
from boba.messaging import CatalogChanged, ChangeAction, MessageBus

__all__ = ["CatalogService"]


class CatalogService:
    """Сценарии каталога от имени субъекта поверх ProcessStore,
    ConnectionStore и шины."""

    FIRST_COMPARABLE_VERSION: ClassVar[int] = 2

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
        self._require_view(subject)

        return await self._processes.list_processes()

    async def process(self, subject: Subject, process_id: UUID) -> Process:
        self._require_view(subject)

        return await self._processes.get_process(process_id)

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
        await self._changed(
            subject, CatalogChanged(draft_id=draft.id, action=ChangeAction.CREATED)
        )

        return draft

    async def open_drafts(self, subject: Subject, process_id: UUID) -> Sequence[Draft]:
        self._require_view(subject)

        return await self._processes.list_drafts(process_id, DraftStatus.OPEN)

    async def my_drafts(self, subject: Subject) -> Sequence[Draft]:
        """Открытые черновики автора по всем процессам: плоский список панели."""
        self._require_view(subject)

        return await self._processes.drafts_of_author(subject.user_id)

    async def rename_draft(self, subject: Subject, draft_id: UUID, name: str) -> Draft:
        self._require_edit(subject)

        draft = await self._processes.rename_draft(draft_id, name)
        await self._changed(
            subject, CatalogChanged(draft_id=draft_id, action=ChangeAction.UPDATED)
        )

        return draft

    async def draft(self, subject: Subject, draft_id: UUID) -> Draft:
        self._require_view(subject)

        return await self._processes.get_draft(draft_id)

    async def draft_state(self, subject: Subject, draft_id: UUID) -> DraftState:
        self._require_view(subject)

        return await self._processes.draft_state(draft_id)

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
        await self._changed(
            subject, CatalogChanged(draft_id=draft_id, action=ChangeAction.UPDATED)
        )

        return await self._processes.draft_state(draft_id)

    async def publish(
        self, subject: Subject, draft_id: UUID, via: AuthorVia
    ) -> Version:
        self._require_edit(subject)

        author = DraftAuthor(user_id=subject.user_id, via=via)
        version = await self._processes.publish(draft_id, author)
        await self._changed(
            subject, CatalogChanged(draft_id=draft_id, action=ChangeAction.DELETED)
        )
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

        await self._changed(
            subject, CatalogChanged(draft_id=draft_id, action=ChangeAction.UPDATED)
        )

        return result

    async def discard_draft(self, subject: Subject, draft_id: UUID) -> Draft:
        self._require_edit(subject)

        draft = await self._processes.discard_draft(draft_id)
        await self._changed(
            subject, CatalogChanged(draft_id=draft_id, action=ChangeAction.DELETED)
        )

        return draft

    async def bump_pins(self, subject: Subject, draft_id: UUID) -> PinBump:
        """Привязки черновика поднимаются до последних версий снимков; что
        после этого перестало сходиться, перечисляется, но не чинится."""
        self._require_edit(subject)

        pins = await self._latest_pins()
        draft = await self._processes.set_pins(draft_id, pins)
        state = await self._processes.draft_state(draft_id)
        resolver = await self._resolver_of(pins)
        violations = tuple(state.snapshot.source_violations(resolver))
        await self._changed(
            subject, CatalogChanged(draft_id=draft_id, action=ChangeAction.UPDATED)
        )

        return PinBump(draft=draft, violations=violations)

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
        """Дети узла дерева снимка с пометками относительно предыдущей версии;
        у первой версии сравнивать не с чем, пометок нет."""
        self._require_view(subject)

        resolved = await self._resolve_version(connection_id, version)
        snapshot = await self._connections.snapshot_of(connection_id, resolved)
        nodes = snapshot.children(connection_id, path)
        if resolved < self.FIRST_COMPARABLE_VERSION:
            return nodes

        diff = await self._connections.diff_of(connection_id, resolved - 1, resolved)
        return list(self._marked(nodes, diff))

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
        self._require_kind(connection.kind)
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

    def _require_kind(self, kind: str) -> None:
        """Ошибки:
        UnknownSourceKindError — снимка этого вида нет в реестре.
        """
        if not self._connections.kinds.known(kind):
            raise UnknownSourceKindError(kind, self._connections.kinds.kinds())

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

    @staticmethod
    def _marked(nodes: Sequence[TreeNode], diff: SourceDiff) -> Iterator[TreeNode]:
        touched = diff.touched_prefixes()
        for node in nodes:
            if node.ref is not None:
                status = diff.status_of(node.ref)
                yield node.model_copy(update={"status": status})
                continue

            if node.path in touched:
                yield node.model_copy(update={"status": ChangeStatus.MODIFIED})
                continue

            yield node

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
