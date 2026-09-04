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

from collections.abc import Sequence
from uuid import UUID

from boba.catalog import CatalogSnapshot, OperationList
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
    RebaseResult,
    ShareTargetKind,
    Version,
    View,
    ViewLayout,
    ViewShare,
    ViewSpec,
    ViewState,
)
from boba.catalog_service.store import CatalogStore
from boba.identity.context import Scope, Subject
from boba.identity.locks import LockToken
from boba.messaging import CatalogChanged, ChangeAction, MessageBus

__all__ = ["CatalogService"]


class CatalogService:
    """Сценарии каталога от имени субъекта поверх CatalogStore и шины."""

    def __init__(
        self, store: CatalogStore, cfg: CatalogConfig, bus: MessageBus
    ) -> None:
        self._store = store
        self._cfg = cfg
        self._bus = bus

    @property
    def store(self) -> CatalogStore:
        return self._store

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
        self._require_edit(subject)

        draft = await self._store.create_draft(name, subject.user_id)
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
        await self._store.append_ops(draft_id, expected_seq, author, ops)
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

        result = await self._store.rebase(draft_id, drop_conflicts=drop_conflicts)
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
            snapshot=snapshot.restricted(view.dataset_ids, view.layer_ids),
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

    async def _changed(self, subject: Subject, message: CatalogChanged) -> None:
        await self._bus.publish(Scope.user(subject.user_id), message, LockToken.local())
