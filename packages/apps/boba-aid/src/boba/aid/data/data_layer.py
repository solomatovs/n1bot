"""PostgresDataLayer chainlit: адаптер BaseDataLayer над портами хранилища чата.

Строки users/threads/feedbacks — у сервиса (порты UserRows, ChatThreads,
FeedbackStore); здесь только перевод в словари chainlit, удаление файлов треда в
хранилище и оповещение шины. Шагов и элементов ленты слой не хранит: историю
диалога вместе с вложениями и итогами вызовов держит агент, лента
воспроизводится из его `session/resume`, а оценки шагов отдаются ленте по id
шага (feedbacks_of).

Ошибки:
DataLayerError — контракт слоя данных: чужое пакуется data_boundary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Coroutine, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar
from uuid import UUID, uuid4

from chainlit.data import get_data_layer
from chainlit.data.base import BaseDataLayer
from chainlit.element import Element as ChainlitElement
from chainlit.element import ElementDict
from chainlit.step import StepDict
from chainlit.types import (
    Feedback as FeedbackPayload,
)
from chainlit.types import (
    FeedbackDict,
    PageInfo,
    PaginatedResponse,
    Pagination,
    ThreadDict,
    ThreadFilter,
)
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser

from boba.aid.domain.fields import ThreadMetaField
from boba.canvas.storage import StorageError
from boba.chat.threads import (
    ChatThreads,
    DataBrokenError,
    DataRejectedError,
    DataUnavailableError,
    FeedbackStore,
    StoredFeedback,
    StoredThread,
    ThreadCursor,
    ThreadCursors,
    ThreadOwnership,
    ThreadUpsert,
    data_boundary,
)
from boba.identity.api import StoredUser, UserRows
from boba.identity.context import Scope
from boba.identity.session import Login
from boba.identity.signin import SignInMetadata
from boba.messaging import (
    AnyMessage,
    ChangeAction,
    FeedbackChanged,
    LockToken,
    MessageBus,
    ThreadActive,
    ThreadChanged,
)
from boba.runtime.storage import StorageClient

__all__ = [
    "ActivityThreadDict",
    "AttachmentDataLayer",
    "FeedRows",
    "PostgresDataLayer",
    "ThreadDeletionListener",
]

_T = TypeVar("_T")


class ActivityThreadDict(ThreadDict):
    """ThreadDict chainlit с временем последней записи истории агента треда:
    по updatedAt панель тредов сортирует и группирует список."""

    updatedAt: str  # noqa: N815 — ключ словаря треда chainlit


class FeedRows(Protocol):
    """Что лента вкладки берёт у слоя данных: тред для очистки вкладки
    перед повторным воспроизведением, оценки шагов по id шага, которые
    она ставит после воспроизведения, и сдвиг активности треда по записи
    истории агента. Реализует PostgresDataLayer."""

    @abstractmethod
    def get_thread(self, thread_id: str) -> Coroutine[Any, Any, ThreadDict | None]:
        """Тред без шагов; None — треда нет."""

    @abstractmethod
    def feedbacks_of(
        self, thread_id: str
    ) -> Coroutine[Any, Any, Mapping[str, FeedbackDict]]:
        """Оценки треда: id шага → оценка."""

    @abstractmethod
    def thread_active(
        self, thread_id: str, updated_at: datetime
    ) -> Coroutine[Any, Any, None]:
        """Агент треда записал историю в updated_at: время треда сдвигается
        вперёд, вкладки владельца получают новое значение."""


class ThreadDeletionListener(Protocol):
    """Кому сообщать об удаляемом треде: слой данных зовёт до снятия строк с
    metadata треда; отказ слушателя оставляет тред. Реализует ThreadDeleter
    клиента агента."""

    @abstractmethod
    async def thread_deleted(self, thread_id: str, meta: Mapping[str, Any]) -> None:
        """Тред удаляется вместе с элементами и оценками."""


class AttachmentDataLayer(BaseDataLayer, ABC):
    """Data layer с хранилищем файлов пользователя: через него маршрут
    загрузки пишет вложения вопроса."""

    @property
    @abstractmethod
    def storage(self) -> StorageClient: ...

    @classmethod
    def require(cls) -> AttachmentDataLayer:
        """Слой данных приложения, адресующий вложения; иной — ошибка сборки."""
        layer = get_data_layer()
        if not isinstance(layer, cls):
            msg = (
                f"attachments expect a {cls.__name__} data layer, "
                f"got {type(layer).__name__}"
            )
            raise RuntimeError(msg)

        return layer


class Codec:
    """Перевод значений между словарями chainlit и строками сервиса."""

    @staticmethod
    def require(value: _T | None, field: str) -> _T:
        if value is None:
            msg = f"chainlit dict field {field!r} is required, got None"
            raise DataBrokenError("codec", msg)

        return value

    @staticmethod
    def uuid(value: str | None, field: str) -> UUID:
        return UUID(Codec.require(value, field))

    @staticmethod
    def uuid_opt(value: str | None) -> UUID | None:
        if value is None:
            return None

        return UUID(value)

    @staticmethod
    def uuid_str(value: UUID) -> str:
        return str(value)

    @staticmethod
    def uuid_str_opt(value: UUID | None) -> str | None:
        if value is None:
            return None

        return str(value)

    @staticmethod
    def iso(value: datetime) -> str:
        return value.isoformat()

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def text_opt(value: str) -> str | None:
        """Пустая строка сервиса — отсутствующее поле словаря chainlit."""
        if not value:
            return None

        return value

    @staticmethod
    def text(value: str | None) -> str:
        if value is None:
            return ""

        return value


class ThreadDicts:
    """Строки сервиса -> словари chainlit: тред и пользователь."""

    @staticmethod
    def thread(stored: StoredThread, user_identifier: str | None) -> ActivityThreadDict:
        """Тред без шагов и элементов: их воспроизводит агент."""
        tags = None
        if stored.tags:
            tags = list(stored.tags)

        return ActivityThreadDict(
            id=Codec.uuid_str(stored.id),
            createdAt=Codec.iso(stored.created_at),
            updatedAt=Codec.iso(stored.updated_at),
            name=Codec.text_opt(stored.name),
            userId=Codec.uuid_str_opt(stored.user_id),
            userIdentifier=user_identifier,
            tags=tags,
            metadata=dict(stored.meta),
            steps=[],
            elements=[],
        )

    @staticmethod
    def user(stored: StoredUser) -> PersistedUser:
        return PersistedUser(
            id=str(stored.id),
            identifier=stored.identifier,
            createdAt=Codec.iso(stored.created_at),
            metadata=dict(stored.meta),
        )

    @staticmethod
    def action(inserted: bool) -> ChangeAction:
        if inserted:
            return ChangeAction.CREATED

        return ChangeAction.UPDATED


class FeedbackDicts:
    """Оценка chainlit <-> строка feedbacks сервиса."""

    @staticmethod
    def stored(payload: FeedbackPayload) -> StoredFeedback:
        feedback_id = Codec.uuid_opt(payload.id)
        if feedback_id is None:
            feedback_id = uuid4()

        return StoredFeedback(
            id=feedback_id,
            for_id=Codec.uuid(payload.forId, "forId"),
            thread_id=Codec.uuid_opt(payload.threadId),
            value=payload.value,
            comment=Codec.text(payload.comment),
        )

    @staticmethod
    def dict_of(stored: StoredFeedback) -> FeedbackDict:
        return FeedbackDict(
            forId=Codec.uuid_str(stored.for_id),
            id=Codec.uuid_str(stored.id),
            value=FeedbackDicts._value(stored.value),
            comment=Codec.text_opt(stored.comment),
        )

    @staticmethod
    def _value(value: int) -> Any:
        if value not in (0, 1):
            msg = f"stored feedback value is expected to be 0 or 1, got {value!r}"
            raise DataBrokenError("feedback", msg)

        return value


class PostgresDataLayer(AttachmentDataLayer, ThreadOwnership, FeedRows):
    """Хранилище chainlit над портами сервиса: словари, тела вложений, оповещения."""

    def __init__(
        self,
        users: UserRows,
        threads: ChatThreads,
        feedbacks: FeedbackStore,
        storage: StorageClient,
        bus: MessageBus,
    ) -> None:
        self._users = users
        self._threads = threads
        self._feedbacks = feedbacks
        self._storage = storage
        self._bus = bus
        self._cursors = ThreadCursors()
        self._deletion_listeners: list[ThreadDeletionListener] = []

    def on_thread_deleted(self, listener: ThreadDeletionListener) -> None:
        """Подписывает слушателя на удаление тредов."""
        self._deletion_listeners.append(listener)

    @property
    def storage(self) -> StorageClient:
        return self._storage

    @data_boundary
    async def get_user(self, identifier: str) -> PersistedUser | None:
        # граница chainlit: identifier из его JWT приходит как набран
        stored = await self._users.stored(Login(identifier))
        if stored is None:
            return None

        return ThreadDicts.user(stored)

    @data_boundary
    async def create_user(self, user: ChainlitUser) -> PersistedUser | None:
        # в строку users уходит только вход без билета; настройки строки не трогаются
        sign_in = SignInMetadata.parse(dict(user.metadata))
        stored = await self._users.upsert(
            Login(user.identifier), sign_in.persistable().render()
        )

        return ThreadDicts.user(stored)

    @data_boundary
    async def upsert_feedback(self, feedback: FeedbackPayload) -> str:
        stored = FeedbackDicts.stored(feedback)
        await self._feedbacks.upsert(stored)

        await self._chat_changed(
            stored.thread_id,
            FeedbackChanged(
                step_id=Codec.uuid_str(stored.for_id),
                value=stored.value,
                comment=stored.comment,
            ),
        )
        return Codec.uuid_str(stored.id)

    @data_boundary
    async def delete_feedback(self, feedback_id: str) -> bool:
        deleted = await self._feedbacks.delete(UUID(feedback_id))
        if deleted is None:
            return False

        await self._chat_changed(
            deleted.thread_id,
            FeedbackChanged(step_id=Codec.uuid_str(deleted.for_id), value=None),
        )
        return True

    @data_boundary
    async def create_element(self, element: ChainlitElement) -> None:
        pass

    @data_boundary
    async def get_element(self, thread_id: str, element_id: str) -> ElementDict | None:
        return None

    @data_boundary
    async def delete_element(
        self, element_id: str, thread_id: str | None = None
    ) -> None:
        pass

    @data_boundary
    async def create_step(self, step_dict: StepDict) -> None:
        pass

    @data_boundary
    async def update_step(self, step_dict: StepDict) -> None:
        pass

    @data_boundary
    async def delete_step(self, step_id: str) -> None:
        pass

    @data_boundary
    async def get_favorite_steps(self, user_id: str) -> list[StepDict]:
        return []

    @data_boundary
    async def get_thread_author(self, thread_id: str) -> str:
        return await self._threads.get_thread_author(thread_id)

    @data_boundary
    async def get_thread(self, thread_id: str) -> ActivityThreadDict | None:
        tid = UUID(thread_id)
        stored = await self._threads.get(tid)
        if stored is None:
            return None

        user_identifier = await self._identifier_of(stored.user_id)

        return ThreadDicts.thread(stored, user_identifier)

    @data_boundary
    async def feedbacks_of(self, thread_id: str) -> Mapping[str, FeedbackDict]:
        """Оценки треда по id шага: лента ставит их шагам после воспроизведения."""
        found: dict[str, FeedbackDict] = {}
        for row in await self._feedbacks.list_of_thread(UUID(thread_id)):
            found[Codec.uuid_str(row.for_id)] = FeedbackDicts.dict_of(row)

        return found

    async def _identifier_of(self, user_id: UUID | None) -> str | None:
        """Логин владельца треда; None — тред без владельца или строки уже нет."""
        if user_id is None:
            return None

        stored = await self._users.stored_by_id(user_id)
        if stored is None:
            return None

        return stored.identifier

    @data_boundary
    async def update_thread(
        self,
        thread_id: str,
        name: str | None = None,
        user_id: str | None = None,
        metadata: dict | None = None,
        tags: list[str] | None = None,
    ) -> None:
        incoming: Mapping[str, Any] = {}
        if metadata is not None:
            incoming = metadata

        meta_set: dict[str, Any] = {}
        meta_del: list[str] = []
        for key, value in incoming.items():
            # тред → сессию агента пишет только агент треда, не persist сессии chainlit
            if key == ThreadMetaField.AGENT_SESSION:
                continue

            if value is None:
                meta_del.append(key)
                continue

            meta_set[key] = value

        name_value = meta_set.get("name")
        if name is not None:
            name_value = name

        user_id_value: UUID | None = None
        if user_id:
            user_id_value = UUID(user_id)

        upsert = await self._threads.upsert(
            ThreadUpsert(
                id=UUID(thread_id),
                name=name_value,
                user_id=user_id_value,
                tags=tags,
                meta_set=meta_set,
                meta_del=meta_del,
            )
        )
        # список тредов меняют только создание и имя; правки метаданных списку не видны
        if not upsert.inserted and name is None:
            return

        await self._thread_changed(
            upsert.user_id, thread_id, upsert.name, ThreadDicts.action(upsert.inserted)
        )

    @data_boundary
    async def delete_thread(self, thread_id: str) -> None:
        """Слушатели (сессия агента) идут первыми, затем область треда у
        сервиса — файлы и журналы вызовов: отказ любого из них оставляет тред."""
        tid = UUID(thread_id)
        stored = await self._threads.get(tid)
        if stored is not None:
            for listener in self._deletion_listeners:
                await listener.thread_deleted(thread_id, stored.meta)

            await self._delete_files(stored)

        await self._feedbacks.delete_of_thread(tid)
        owner = await self._threads.delete(tid)

        if owner is not None:
            await self._thread_changed(owner, thread_id, "", ChangeAction.DELETED)

    async def _delete_files(self, stored: StoredThread) -> None:
        """Файлы треда в хранилище его владельца; у треда без владельца
        файлов нет."""
        owner = stored.user_id
        if owner is None:
            return

        try:
            await self._storage.delete_scope(str(owner), str(stored.id))
        except StorageError as exc:
            msg = (
                f"deleting the files of thread {stored.id} of user {owner} "
                f"failed: {exc}"
            )
            raise DataUnavailableError("delete_thread", msg) from exc

    async def _chat_changed(self, thread_id: UUID | None, message: AnyMessage) -> None:
        """Сообщает вкладкам треда на всех инстансах об изменении в его ленте;
        запись без треда никому не показана.
        """
        if thread_id is None:
            return

        scope = Scope.chat(str(thread_id))
        await self._bus.publish(scope, message, LockToken.local())

    async def _thread_changed(
        self, user_id: UUID | None, thread_id: str, name: str, action: ChangeAction
    ) -> None:
        """Сообщает вкладкам пользователя на всех инстансах, что его список тредов
        изменился; тред без владельца в списках не живёт.
        """
        if user_id is None:
            return

        message = ThreadChanged(thread_id=thread_id, name=name, action=action)
        await self._bus.publish(Scope.user(user_id), message, LockToken.local())

    @data_boundary
    async def thread_active(self, thread_id: str, updated_at: datetime) -> None:
        """Время треда сдвигается только вперёд; сдвиг уходит вкладкам
        владельца на всех инстансах значением из базы."""
        touched = await self._threads.touch(UUID(thread_id), updated_at)
        if touched is None:
            return

        owner = touched.user_id
        if owner is None:
            return

        message = ThreadActive(thread_id=thread_id, updated_at=touched.updated_at)
        await self._bus.publish(Scope.user(owner), message, LockToken.local())

    @data_boundary
    async def list_threads(
        self,
        pagination: Pagination,
        filters: ThreadFilter,
    ) -> PaginatedResponse[ActivityThreadDict]:
        """Страница тредов пользователя по последней активности; курсор —
        (updated_at, id) последнего треда страницы."""
        if not filters.userId:
            msg = f"filters.userId is required, got {filters.userId!r}"
            raise DataRejectedError("list_threads", msg)

        user_id = UUID(filters.userId)
        after: ThreadCursor | None = None
        if pagination.cursor:
            after = self._cursors.parse(pagination.cursor)

        user_identifier = await self._identifier_of(user_id)
        rows = await self._threads.list_of(user_id, pagination.first + 1, after)

        has_next = len(rows) > pagination.first
        shown = rows[: pagination.first]

        page: list[ActivityThreadDict] = []
        for stored in shown:
            page.append(ThreadDicts.thread(stored, user_identifier))

        start_cursor = None
        end_cursor = None
        if shown:
            start_cursor = self._cursor_of(shown[0])
            end_cursor = self._cursor_of(shown[-1])

        return PaginatedResponse(
            pageInfo=PageInfo(
                hasNextPage=has_next,
                startCursor=start_cursor,
                endCursor=end_cursor,
            ),
            data=page,
        )

    @staticmethod
    def _cursor_of(stored: StoredThread) -> str:
        return ThreadCursor(updated_at=stored.updated_at, id=stored.id).render()

    @data_boundary
    async def build_debug_url(self) -> str:
        return ""

    @data_boundary
    async def close(self) -> None:
        await self._storage.close()
