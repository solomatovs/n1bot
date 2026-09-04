"""Сервис каталога: права субъекта, сценарии над хранилищем, события шины.

Единственная точка входа для JSON API и инструментов LLM: оба зовут одни и
те же методы с Subject. Чтение каталога открыто ролям view_roles и
edit_roles, вид дополнительно открыт его владельцу и тем, кому он расшарен;
правки, черновики и публикация — только edit_roles. После каждой правки в
область пользователя уходит CatalogChanged.

Ошибки:
CatalogRefusalError — у субъекта нет прав на действие.
CatalogStoreError, DraftNotFoundError, DraftClosedError, DraftConflictError,
    DraftStaleError, ViewNotFoundError — как у CatalogStore.
CatalogOpError — порция операций не применима к снимку черновика.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import ClassVar
from uuid import UUID

from boba.catalog import (
    CatalogError,
    CatalogSnapshot,
    ChangeStatus,
    ObjectCard,
    ObjectCards,
    ObjectKind,
    ObjectRef,
    OperationList,
    PinnedSnapshot,
    SnapshotResolver,
    SourceDiff,
    SourceOperationList,
    SourceSnapshot,
    Staleness,
    TreeNode,
)
from boba.catalog_service.config import CatalogConfig
from boba.catalog_service.records import (
    AuthorVia,
    CatalogAccess,
    CatalogRefusalError,
    CatalogRefusalKind,
    Draft,
    DraftAuthor,
    DraftState,
    DraftStatus,
    NodePosition,
    PinBump,
    RebaseResult,
    ShareTargetKind,
    Source,
    SourceConnection,
    SourceDraft,
    SourceDraftState,
    SourceObjectNotFoundError,
    SourceSpec,
    SourceVersion,
    Version,
    VersionOrigin,
    View,
    ViewLayout,
    ViewShare,
    ViewSpec,
    ViewState,
)
from boba.catalog_service.source_store import SourceStore
from boba.catalog_service.store import CatalogStore
from boba.identity.context import Scope, Subject
from boba.identity.locks import LockToken
from boba.messaging import CatalogChanged, ChangeAction, MessageBus

__all__ = ["CatalogService"]


class CatalogService:
    """Сценарии каталога от имени субъекта поверх CatalogStore, SourceStore и
    шины."""

    FIRST_COMPARABLE_VERSION: ClassVar[int] = 2

    def __init__(
        self,
        store: CatalogStore,
        sources: SourceStore,
        cfg: CatalogConfig,
        bus: MessageBus,
    ) -> None:
        self._store = store
        self._sources = sources
        self._cfg = cfg
        self._bus = bus

    @property
    def store(self) -> CatalogStore:
        return self._store

    @property
    def sources(self) -> SourceStore:
        return self._sources

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

    async def snapshot(self, subject: Subject) -> CatalogSnapshot:
        self._require_view(subject)

        return await self._store.snapshot()

    async def versions(self, subject: Subject) -> Sequence[Version]:
        self._require_view(subject)

        return await self._store.versions()

    async def create_draft(self, subject: Subject, name: str) -> Draft:
        """Черновик над текущей версией, привязанный к последним версиям всех
        источников на момент создания."""
        self._require_edit(subject)

        pins = await self._latest_pins()
        draft = await self._store.create_draft(name, subject.user_id, pins)
        await self._changed(
            subject, CatalogChanged(draft_id=draft.id, action=ChangeAction.CREATED)
        )

        return draft

    async def open_drafts(self, subject: Subject) -> Sequence[Draft]:
        self._require_view(subject)

        return await self._store.list_drafts(DraftStatus.OPEN)

    async def draft_state(self, subject: Subject, draft_id: UUID) -> DraftState:
        self._require_view(subject)

        return await self._store.draft_state(draft_id)

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
        draft = await self._store.get_draft(draft_id)
        resolver = await self._resolver_of(draft.pins)
        await self._store.append_ops(draft_id, expected_seq, author, ops, resolver)
        await self._changed(
            subject, CatalogChanged(draft_id=draft_id, action=ChangeAction.UPDATED)
        )

        return await self._store.draft_state(draft_id)

    async def publish(
        self, subject: Subject, draft_id: UUID, via: AuthorVia
    ) -> Version:
        self._require_edit(subject)

        author = DraftAuthor(user_id=subject.user_id, via=via)
        version = await self._store.publish(draft_id, author)
        await self._changed(
            subject, CatalogChanged(draft_id=draft_id, action=ChangeAction.DELETED)
        )
        await self._changed(
            subject, CatalogChanged(version=version.number, action=ChangeAction.CREATED)
        )

        return version

    async def rebase(
        self, subject: Subject, draft_id: UUID, *, drop_conflicts: bool
    ) -> RebaseResult:
        self._require_edit(subject)

        draft = await self._store.get_draft(draft_id)
        resolver = await self._resolver_of(draft.pins)
        result = await self._store.rebase(
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

        draft = await self._store.discard_draft(draft_id)
        await self._changed(
            subject, CatalogChanged(draft_id=draft_id, action=ChangeAction.DELETED)
        )

        return draft

    async def published_pins(self, subject: Subject) -> Mapping[UUID, int]:
        """Привязки последней версии процесса; без версий — пусто."""
        self._require_view(subject)

        versions = await self._store.versions()
        if not versions:
            return {}

        return versions[-1].pins

    async def resolver_of(
        self, subject: Subject, pins: Mapping[UUID, int]
    ) -> SnapshotResolver:
        """Резолвер объектов по привязанным версиям источников."""
        self._require_view(subject)

        return await self._resolver_of(pins)

    async def staleness(self, subject: Subject) -> Staleness:
        """Устаревание опубликованного процесса относительно последних версий
        источников, по привязкам последней версии процесса."""
        self._require_view(subject)

        versions = await self._store.versions()
        if not versions:
            return Staleness(entries=())

        snapshot = await self._store.snapshot()
        return await self._staleness_of(snapshot, versions[-1].pins)

    async def draft_staleness(self, subject: Subject, draft_id: UUID) -> Staleness:
        self._require_view(subject)

        state = await self._store.draft_state(draft_id)
        return await self._staleness_of(state.snapshot, state.draft.pins)

    async def bump_pins(self, subject: Subject, draft_id: UUID) -> PinBump:
        """Привязки черновика поднимаются до последних версий источников; что
        после этого перестало сходиться, перечисляется, но не чинится."""
        self._require_edit(subject)

        pins = await self._latest_pins()
        draft = await self._store.set_pins(draft_id, pins)
        state = await self._store.draft_state(draft_id)
        resolver = await self._resolver_of(pins)
        violations = tuple(state.snapshot.source_violations(resolver))
        await self._changed(
            subject, CatalogChanged(draft_id=draft_id, action=ChangeAction.UPDATED)
        )

        return PinBump(draft=draft, violations=violations)

    async def _latest_pins(self) -> dict[UUID, int]:
        pins: dict[UUID, int] = {}
        for source in await self._sources.list_sources():
            if source.latest_version == 0:
                continue

            pins[source.id] = source.latest_version

        return pins

    async def _resolver_of(self, pins: Mapping[UUID, int]) -> SnapshotResolver:
        snapshots: dict[UUID, SourceSnapshot] = {}
        for source_id, version in pins.items():
            snapshots[source_id] = await self._sources.snapshot_of(source_id, version)

        return SnapshotResolver(snapshots)

    async def _staleness_of(
        self, snapshot: CatalogSnapshot, pins: Mapping[UUID, int]
    ) -> Staleness:
        pinned: dict[UUID, PinnedSnapshot] = {}
        latest: dict[UUID, PinnedSnapshot] = {}
        for source_id in snapshot.sources():
            pinned_version = pins.get(source_id)
            if pinned_version is None:
                continue

            source = await self._sources.get_source(source_id)
            if source.latest_version == pinned_version:
                continue

            pinned[source_id] = PinnedSnapshot(
                version=pinned_version,
                snapshot=await self._sources.snapshot_of(source_id, pinned_version),
            )
            latest[source_id] = PinnedSnapshot(
                version=source.latest_version,
                snapshot=await self._sources.latest_snapshot(source_id),
            )

        return Staleness.compute(snapshot, pinned, latest)

    async def views(self, subject: Subject) -> Sequence[View]:
        """Виды субъекта: все при праве на каталог, иначе свои и расшаренные."""
        everything = self.can_view(subject)
        roles = sorted(subject.roles)

        return await self._store.views_for(
            subject.user_id, roles, everything=everything
        )

    async def view(self, subject: Subject, view_id: UUID) -> View:
        return await self._accessible_view(subject, view_id)

    async def view_state(self, subject: Subject, view_id: UUID) -> ViewState:
        """Страница вида для владельца, читателя каталога и того, кому вид
        расшарен: снимок обрезан по фильтру вида, права на каталог не нужны."""
        view = await self._accessible_view(subject, view_id)

        snapshot = await self._store.snapshot()
        version = await self._store.current_version()
        layout = await self._store.layout_of(view_id)

        owned = False
        if view.owner_id == subject.user_id:
            owned = self.can_edit(subject)

        return ViewState(
            view=view,
            version=version,
            snapshot=snapshot.restricted(view.node_ids, view.layer_ids),
            layout=layout,
            owned=owned,
        )

    def access(self, subject: Subject) -> CatalogAccess:
        return CatalogAccess(
            user_id=subject.user_id,
            login=subject.login,
            can_view=self.can_view(subject),
            can_edit=self.can_edit(subject),
        )

    async def create_view(self, subject: Subject, spec: ViewSpec) -> View:
        self._require_edit(subject)

        view = await self._store.create_view(subject.user_id, spec)
        await self._changed(
            subject, CatalogChanged(view_id=view.id, action=ChangeAction.CREATED)
        )

        return view

    async def update_view(
        self, subject: Subject, view_id: UUID, spec: ViewSpec
    ) -> View:
        await self._owned_view(subject, view_id)

        view = await self._store.update_view(view_id, spec)
        await self._changed(
            subject, CatalogChanged(view_id=view_id, action=ChangeAction.UPDATED)
        )

        return view

    async def delete_view(self, subject: Subject, view_id: UUID) -> bool:
        await self._owned_view(subject, view_id)

        deleted = await self._store.delete_view(view_id)
        if not deleted:
            return False

        await self._changed(
            subject, CatalogChanged(view_id=view_id, action=ChangeAction.DELETED)
        )

        return True

    async def layout(self, subject: Subject, view_id: UUID) -> ViewLayout:
        await self._accessible_view(subject, view_id)

        return await self._store.layout_of(view_id)

    async def put_layout(
        self, subject: Subject, view_id: UUID, positions: Sequence[NodePosition]
    ) -> ViewLayout:
        await self._owned_view(subject, view_id)

        layout = await self._store.put_layout(view_id, positions)
        await self._changed(
            subject, CatalogChanged(view_id=view_id, action=ChangeAction.UPDATED)
        )

        return layout

    async def shares(self, subject: Subject, view_id: UUID) -> Sequence[ViewShare]:
        await self._owned_view(subject, view_id)

        return await self._store.shares_of(view_id)

    async def share_view(
        self, subject: Subject, view_id: UUID, share: ViewShare
    ) -> None:
        await self._owned_view(subject, view_id)

        await self._store.share_view(view_id, share)
        await self._changed(
            subject, CatalogChanged(view_id=view_id, action=ChangeAction.UPDATED)
        )

    async def unshare_view(
        self, subject: Subject, view_id: UUID, share: ViewShare
    ) -> bool:
        await self._owned_view(subject, view_id)

        removed = await self._store.unshare_view(view_id, share)
        if not removed:
            return False

        await self._changed(
            subject, CatalogChanged(view_id=view_id, action=ChangeAction.UPDATED)
        )

        return True

    def _require_view(self, subject: Subject) -> None:
        if self.can_view(subject):
            return

        msg = f"user {subject.login!r} has no role to read the catalog"
        raise CatalogRefusalError(CatalogRefusalKind.VIEW_FORBIDDEN, msg)

    def _require_edit(self, subject: Subject) -> None:
        if self.can_edit(subject):
            return

        msg = f"user {subject.login!r} has no role to edit the catalog"
        raise CatalogRefusalError(CatalogRefusalKind.EDIT_FORBIDDEN, msg)

    async def _accessible_view(self, subject: Subject, view_id: UUID) -> View:
        """Вид открыт при праве на каталог, владельцу и по шарингу."""
        view = await self._store.get_view(view_id)
        if self.can_view(subject):
            return view

        if view.owner_id == subject.user_id:
            return view

        shares = await self._store.shares_of(view_id)
        for share in shares:
            if self._share_covers(share, subject):
                return view

        msg = f"user {subject.login!r} has no access to view {view.name!r}"
        raise CatalogRefusalError(CatalogRefusalKind.VIEW_FORBIDDEN, msg)

    async def _owned_view(self, subject: Subject, view_id: UUID) -> View:
        """Править вид может его владелец с правом на правки каталога."""
        self._require_edit(subject)

        view = await self._store.get_view(view_id)
        if view.owner_id == subject.user_id:
            return view

        msg = f"user {subject.login!r} does not own view {view.name!r}"
        raise CatalogRefusalError(CatalogRefusalKind.NOT_OWNER, msg)

    @staticmethod
    def _share_covers(share: ViewShare, subject: Subject) -> bool:
        if share.kind is ShareTargetKind.USER:
            return share.target == str(subject.user_id)

        return share.target in subject.roles

    # --- источники ---

    async def list_sources(self, subject: Subject) -> Sequence[Source]:
        self._require_view(subject)

        return await self._sources.list_sources()

    async def source(self, subject: Subject, source_id: UUID) -> Source:
        self._require_view(subject)

        return await self._sources.get_source(source_id)

    async def create_source(self, subject: Subject, spec: SourceSpec) -> Source:
        self._require_edit(subject)

        source = await self._sources.create_source(spec, subject.user_id)
        await self._source_changed(subject, source.id, ChangeAction.CREATED)

        return source

    async def update_source(
        self, subject: Subject, source_id: UUID, spec: SourceSpec
    ) -> Source:
        self._require_edit(subject)

        source = await self._sources.update_source(source_id, spec)
        await self._source_changed(subject, source_id, ChangeAction.UPDATED)

        return source

    async def delete_source(self, subject: Subject, source_id: UUID) -> bool:
        self._require_edit(subject)

        deleted = await self._sources.delete_source(source_id)
        if not deleted:
            return False

        await self._source_changed(subject, source_id, ChangeAction.DELETED)

        return True

    async def source_connections(
        self, subject: Subject, source_id: UUID
    ) -> Sequence[SourceConnection]:
        self._require_view(subject)

        return await self._sources.connections_of(source_id)

    async def bind_connection(
        self, subject: Subject, source_id: UUID, connection_id: UUID
    ) -> SourceConnection:
        self._require_edit(subject)

        bound = await self._sources.bind_connection(
            source_id, connection_id, subject.user_id
        )
        await self._source_changed(subject, source_id, ChangeAction.UPDATED)

        return bound

    async def unbind_connection(
        self, subject: Subject, source_id: UUID, connection_id: UUID
    ) -> bool:
        self._require_edit(subject)

        removed = await self._sources.unbind_connection(source_id, connection_id)
        if not removed:
            return False

        await self._source_changed(subject, source_id, ChangeAction.UPDATED)

        return True

    async def source_versions(
        self, subject: Subject, source_id: UUID
    ) -> Sequence[SourceVersion]:
        self._require_view(subject)

        return await self._sources.versions_of(source_id)

    async def source_snapshot(
        self, subject: Subject, source_id: UUID, version: int
    ) -> SourceSnapshot:
        """Снимок версии; version 0 — пустой снимок, отрицательная — последняя."""
        self._require_view(subject)

        if version < 0:
            return await self._sources.latest_snapshot(source_id)

        return await self._sources.snapshot_of(source_id, version)

    async def source_tree(
        self, subject: Subject, source_id: UUID, version: int, path: Sequence[str]
    ) -> Sequence[TreeNode]:
        """Дети узла дерева источника с пометками относительно предыдущей версии;
        у первой версии сравнивать не с чем, пометок нет."""
        self._require_view(subject)

        source = await self._sources.get_source(source_id)
        resolved = self._resolve_version(source, version)
        snapshot = await self._sources.snapshot_of(source_id, resolved)
        nodes = snapshot.children(source_id, path)
        if resolved < self.FIRST_COMPARABLE_VERSION:
            return nodes

        diff = await self._sources.diff_of(source_id, resolved - 1, resolved)
        return list(self._marked(nodes, diff))

    async def source_object(
        self, subject: Subject, ref: ObjectRef, version: int
    ) -> ObjectCard:
        """Карточка объекта по адресу в версии источника (отрицательная — последняя).

        Ошибки:
        SourceObjectNotFoundError — по адресу нет объекта.
        """
        self._require_view(subject)

        source = await self._sources.get_source(ref.source_id)
        resolved = self._resolve_version(source, version)
        snapshot = await self._sources.snapshot_of(ref.source_id, resolved)
        try:
            return ObjectCards.of(snapshot, ref)
        except CatalogError as exc:
            raise SourceObjectNotFoundError(ref) from exc

    async def source_diff(
        self, subject: Subject, source_id: UUID, old: int, new: int
    ) -> SourceDiff:
        self._require_view(subject)

        return await self._sources.diff_of(source_id, old, new)

    async def write_source_version(
        self, subject: Subject, source_id: UUID, snapshot: SourceSnapshot
    ) -> SourceVersion:
        """Версия целиком от имени субъекта: путь стенда и переноса из staging."""
        self._require_edit(subject)

        origin = VersionOrigin(taken_by=subject.user_id)
        version = await self._sources.write_version(source_id, snapshot, origin)
        await self._source_changed(subject, source_id, ChangeAction.UPDATED)

        return version

    # --- черновики ручного источника ---

    async def source_drafts(
        self, subject: Subject, source_id: UUID
    ) -> Sequence[SourceDraft]:
        self._require_view(subject)

        return await self._sources.open_drafts(source_id)

    async def create_source_draft(
        self, subject: Subject, source_id: UUID, name: str
    ) -> SourceDraft:
        self._require_edit(subject)

        draft = await self._sources.create_draft(source_id, name, subject.user_id)
        await self._source_changed(subject, source_id, ChangeAction.UPDATED)

        return draft

    async def source_draft_state(
        self, subject: Subject, draft_id: UUID
    ) -> SourceDraftState:
        self._require_view(subject)

        return await self._sources.draft_state(draft_id)

    async def source_draft_tree(
        self, subject: Subject, draft_id: UUID, path: Sequence[str]
    ) -> Sequence[TreeNode]:
        """Дерево свёрнутого снимка черновика с пометками относительно его базы."""
        self._require_view(subject)

        state = await self._sources.draft_state(draft_id)
        nodes = state.snapshot.children(state.draft.source_id, path)
        return list(self._marked(nodes, state.diff))

    async def source_draft_object(
        self, subject: Subject, draft_id: UUID, kind: ObjectKind, path: Sequence[str]
    ) -> ObjectCard:
        """Ошибки:
        SourceObjectNotFoundError — по адресу нет объекта.
        """
        self._require_view(subject)

        state = await self._sources.draft_state(draft_id)
        ref = ObjectRef(source_id=state.draft.source_id, kind=kind, path=tuple(path))
        try:
            return ObjectCards.of(state.snapshot, ref)
        except CatalogError as exc:
            raise SourceObjectNotFoundError(ref) from exc

    async def append_source_ops(
        self,
        subject: Subject,
        draft_id: UUID,
        expected_seq: int,
        operations: SourceOperationList,
        via: AuthorVia,
    ) -> SourceDraftState:
        self._require_edit(subject)

        author = DraftAuthor(user_id=subject.user_id, via=via)
        state = await self._sources.append_ops(
            draft_id, expected_seq, operations, author
        )
        await self._source_changed(subject, state.draft.source_id, ChangeAction.UPDATED)

        return state

    async def publish_source_draft(
        self, subject: Subject, draft_id: UUID, via: AuthorVia
    ) -> SourceVersion:
        self._require_edit(subject)

        author = DraftAuthor(user_id=subject.user_id, via=via)
        version = await self._sources.publish_draft(draft_id, author)
        await self._source_changed(subject, version.source_id, ChangeAction.UPDATED)

        return version

    async def discard_source_draft(
        self, subject: Subject, draft_id: UUID
    ) -> SourceDraft:
        self._require_edit(subject)

        draft = await self._sources.discard_draft(draft_id)
        await self._source_changed(subject, draft.source_id, ChangeAction.UPDATED)

        return draft

    @staticmethod
    def _resolve_version(source: Source, version: int) -> int:
        if version < 0:
            return source.latest_version

        return version

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

    async def _source_changed(
        self, subject: Subject, source_id: UUID, action: ChangeAction
    ) -> None:
        await self._changed(subject, CatalogChanged(source_id=source_id, action=action))

    async def _changed(self, subject: Subject, message: CatalogChanged) -> None:
        await self._bus.publish(Scope.user(subject.user_id), message, LockToken.local())
