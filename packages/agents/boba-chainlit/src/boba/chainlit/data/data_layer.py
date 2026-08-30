"""PostgresDataLayer chainlit: адаптер BaseDataLayer над портами хранилища чата.

Строки users/threads/elements/feedbacks — у сервиса (порты UserRows, ChatThreads,
ElementStore, FeedbackStore); здесь только перевод в словари chainlit, тела вложений
в хранилище файлов и оповещение шины. Сообщения диалога хранит checkpointer.

Ошибки:
DataLayerError — контракт слоя данных: чужое пакуется data_boundary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar
from uuid import UUID, uuid4

import aiofiles
import aiofiles.os

from boba.canvas.journal import StreamJournalError, StreamJournalHub
from boba.canvas.keys import ElementProps, ObjectKey
from boba.chainlit.data.storage import StorageClient
from boba.chainlit.domain.fields import ElementField, StepField, ThreadField
from boba.chainlit.domain.keys import AttachmentLinks
from boba.chat.threads import (
    ChatThreads,
    DataBrokenError,
    DataRejectedError,
    DataUnavailableError,
    ElementStore,
    FeedbackStore,
    StoredElement,
    StoredFeedback,
    StoredThread,
    ThreadOwnership,
    ThreadUpsert,
    data_boundary,
)
from boba.identity.api import StoredUser, UserRows
from boba.identity.context import Scope
from boba.identity.session import SessionSource
from boba.messaging import (
    AnyMessage,
    ChangeAction,
    ElementRemoved,
    FeedbackChanged,
    LockToken,
    MessageBus,
    ThreadChanged,
)
from chainlit.data import get_data_layer
from chainlit.data.base import BaseDataLayer
from chainlit.data.utils import queue_until_user_message
from chainlit.element import CustomElement, ElementDict
from chainlit.element import Element as ChainlitElement
from chainlit.logger import logger
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

__all__ = [
    "AttachmentDataLayer",
    "PostgresDataLayer",
]

_T = TypeVar("_T")


class ThreadFeed(Protocol):
    """Сборщик ленты треда: слой данных знает контракт, но не реализацию.

    Историю разворачивает в шаги слой чата — иначе хранилище зависело бы от
    отрисовки.
    """

    async def steps(
        self, thread_id: str, user_name: str | None
    ) -> Sequence[StepDict]: ...


class AttachmentDataLayer(BaseDataLayer, ABC):
    """Data layer, умеющий адресовать вложения публичными ссылками."""

    @property
    @abstractmethod
    def links(self) -> AttachmentLinks: ...

    @property
    @abstractmethod
    def storage(self) -> StorageClient: ...

    @classmethod
    def require(cls) -> AttachmentDataLayer:
        """Слой данных приложения, адресующий вложения; иной — ошибка сборки."""
        layer = get_data_layer()
        if not isinstance(layer, cls):
            msg = f"data layer does not address attachments: {type(layer)}"
            raise RuntimeError(msg)

        return layer


class Codec:
    """Перевод значений между словарями chainlit и строками сервиса."""

    @staticmethod
    def require(value: _T | None) -> _T:
        if value is None:
            raise DataBrokenError("codec", "required value is missing")

        return value

    @staticmethod
    def uuid(value: str | None) -> UUID:
        return UUID(Codec.require(value))

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
    def thread(
        stored: StoredThread,
        user_identifier: str | None,
        steps: list[StepDict],
        elements: list[ElementDict],
    ) -> ThreadDict:
        tags = None
        if stored.tags:
            tags = list(stored.tags)

        return ThreadDict(
            id=Codec.uuid_str(stored.id),
            createdAt=Codec.iso(stored.created_at),
            name=Codec.text_opt(stored.name),
            userId=Codec.uuid_str_opt(stored.user_id),
            userIdentifier=user_identifier,
            tags=tags,
            metadata=dict(stored.meta),
            steps=steps,
            elements=elements,
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


class ElementDicts:
    """Словарь элемента chainlit <-> строка elements сервиса."""

    @staticmethod
    def stored(data: ElementDict) -> StoredElement:
        props = data.get(ElementField.PROPS)
        if props is None:
            props = {}

        return StoredElement(
            id=Codec.uuid(data.get(ElementField.ID)),
            thread_id=Codec.uuid_opt(data.get(ElementField.THREAD_ID)),
            for_id=Codec.uuid_opt(data.get(ElementField.FOR_ID)),
            type=Codec.require(data.get(ElementField.TYPE)),
            chainlit_key=Codec.text(data.get(ElementField.CHAINLIT_KEY)),
            name=Codec.require(data.get(ElementField.NAME)),
            display=Codec.require(data.get(ElementField.DISPLAY)),
            size=Codec.text(data.get(ElementField.SIZE)),
            language=Codec.text(data.get(ElementField.LANGUAGE)),
            page=data.get(ElementField.PAGE),
            props=props,
            mime=Codec.text(data.get(ElementField.MIME)),
        )

    @staticmethod
    def dict_of(stored: StoredElement) -> ElementDict:
        props = None
        if stored.props:
            props = dict(stored.props)

        data: dict[str, Any] = {
            ElementField.ID: Codec.uuid_str(stored.id),
            ElementField.THREAD_ID: Codec.uuid_str_opt(stored.thread_id),
            ElementField.TYPE: stored.type,
            ElementField.CHAINLIT_KEY: Codec.text_opt(stored.chainlit_key),
            ElementField.NAME: stored.name,
            ElementField.DISPLAY: stored.display,
            ElementField.SIZE: Codec.text_opt(stored.size),
            ElementField.LANGUAGE: Codec.text_opt(stored.language),
            ElementField.PAGE: stored.page,
            ElementField.PROPS: props,
            ElementField.FOR_ID: Codec.uuid_str_opt(stored.for_id),
            ElementField.MIME: Codec.text_opt(stored.mime),
        }
        element: ElementDict = data  # type: ignore[assignment]

        return element


class FeedbackDicts:
    """Оценка chainlit <-> строка feedbacks сервиса."""

    @staticmethod
    def stored(payload: FeedbackPayload) -> StoredFeedback:
        feedback_id = Codec.uuid_opt(payload.id)
        if feedback_id is None:
            feedback_id = uuid4()

        return StoredFeedback(
            id=feedback_id,
            for_id=Codec.uuid(payload.forId),
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
            raise DataBrokenError("feedback", f"feedback value {value} is not 0 or 1")

        return value


class PostgresDataLayer(AttachmentDataLayer, ThreadOwnership):
    """Хранилище chainlit над портами сервиса: словари, тела вложений, оповещения."""

    def __init__(  # noqa: PLR0913 — зависимости слоя вносятся сборкой
        self,
        users: UserRows,
        threads: ChatThreads,
        elements: ElementStore,
        feedbacks: FeedbackStore,
        storage: StorageClient,
        feed: ThreadFeed,
        links: AttachmentLinks,
        sessions: SessionSource,
        bus: MessageBus,
    ) -> None:
        self._users = users
        self._threads = threads
        self._elements = elements
        self._feedbacks = feedbacks
        self._storage = storage
        self._feed = feed
        self._links = links
        self._sessions = sessions
        self._bus = bus

    @property
    def links(self) -> AttachmentLinks:
        return self._links

    @property
    def storage(self) -> StorageClient:
        return self._storage

    @data_boundary
    async def get_user(self, identifier: str) -> PersistedUser | None:
        stored = await self._users.stored(identifier)
        if stored is None:
            return None

        return ThreadDicts.user(stored)

    @data_boundary
    async def create_user(self, user: ChainlitUser) -> PersistedUser | None:
        # копия metadata: строка правит своё поле, а не словарь вызывающего
        stored = await self._users.upsert(user.identifier, dict(user.metadata))

        return ThreadDicts.user(stored)

    @data_boundary
    async def update_user_llm_settings(
        self,
        user_id: UUID,
        profile: str,
        values: Mapping[str, Any],
    ) -> None:
        """Настройки LLM пользователя для профиля; пустые значения снимают ключ."""
        await self._users.set_llm_settings(user_id, profile, values)

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

    async def _store_element_body(
        self, element: ChainlitElement, object_key: str, mime: str
    ) -> None:
        """content уходит как есть, файл с диска — потоком; нет ни того ни
        другого — содержимое уже залил в хранилище стриминговый роут."""
        if self._props_only(element):
            return

        if element.content is not None:
            uploaded = await self._storage.upload_file(
                object_key=object_key,
                data=element.content,
                mime=mime,
                overwrite=True,
            )
            self._require_uploaded(uploaded)
            return

        on_disk = False
        if element.path:
            on_disk = await aiofiles.os.path.exists(element.path)

        if on_disk and element.path:
            source = self._storage.disk_source(element.path)
            uploaded = await self._storage.upload_stream(object_key, source, mime)
            self._require_uploaded(uploaded)
            return

        logger.info("element %s is already in storage as %s", element.id, object_key)

    @staticmethod
    def _props_only(element: ChainlitElement) -> bool:
        """Кастом-элемент несёт только props: тело в хранилище ему не нужно.

        chainlit кладёт в content те же props json-строкой, а лента берёт их
        из колонки props — копия в образе никем не читается, зато каждая
        стоит монтирования fuse-образа пользователя.
        """
        if not isinstance(element, CustomElement):
            return False

        return not element.path

    @staticmethod
    def _require_uploaded(uploaded: Mapping[str, object]) -> None:
        if not uploaded:
            raise DataUnavailableError("create_element", "storage refused the upload")

    @queue_until_user_message()
    @data_boundary
    async def create_element(self, element: ChainlitElement) -> None:
        if not element.for_id:
            # панель канваса шлёт непривязанные side-элементы: они живут
            # только в websocket, их непersистентность — норма, не потеря
            if element.display == "side":
                return

            logger.warning(
                "element %s without for_id is not persisted: "
                "it is not attached to anything",
                element.id,
            )
            return
        try:
            await self._create_element(element)
        except Exception as e:
            # chainlit зовёт create_element фоновой таской и молча гасит исключение
            raise DataUnavailableError("create_element", str(e)) from e

    async def _create_element(self, element: ChainlitElement) -> None:
        """Строка описания, затем тело; тело не залилось — строка снимается."""
        user_id = self._session_user_id()

        mime = element.mime or "application/octet-stream"

        data = element.to_dict()
        data[ElementField.MIME] = mime

        stored = ElementDicts.stored(data)
        object_key = ObjectKey.build(
            user_id, element.thread_id, element.name, element.id
        ).render()

        await self._elements.upsert(stored)
        try:
            await self._store_element_body(element, object_key, mime)
        except Exception:
            await self._elements.delete(stored.id)
            raise

    @data_boundary
    async def get_element(self, thread_id: str, element_id: str) -> ElementDict | None:
        stored = await self._elements.get(UUID(thread_id), UUID(element_id))
        if stored is None:
            return None

        element = ElementDicts.dict_of(stored)
        self._sign_element_url(element)
        return element

    @queue_until_user_message()
    @data_boundary
    async def delete_element(
        self, element_id: str, thread_id: str | None = None
    ) -> None:
        """Сначала тело в хранилище, потом строка: строка без тела не остаётся."""
        found = await self._elements.find(UUID(element_id))
        if found is None:
            return

        if found.thread_id is not None:
            user_id = self._session_user_id()
            key = ObjectKey.build(user_id, str(found.thread_id), found.name, element_id)
            try:
                await self._storage.delete_file(object_key=key.render())
            except Exception as e:
                raise DataUnavailableError("delete_element", str(e)) from e

        await self._elements.delete(found.id)

        if found.thread_id is not None:
            removed = ElementRemoved(element_id=element_id)
            await self._chat_changed(found.thread_id, removed)

    @data_boundary
    async def create_step(self, step_dict: StepDict) -> None:
        pass

    @data_boundary
    async def update_step(self, step_dict: StepDict) -> None:
        pass

    @queue_until_user_message()
    @data_boundary
    async def delete_step(self, step_id: str) -> None:
        step = UUID(step_id)
        await self._feedbacks.delete_of_step(step)
        await self._elements.delete_of_step(step)

    @data_boundary
    async def get_favorite_steps(self, user_id: str) -> list[StepDict]:
        return []

    @data_boundary
    async def get_thread_author(self, thread_id: str) -> str:
        return await self._threads.get_thread_author(thread_id)

    @data_boundary
    async def get_thread(self, thread_id: str) -> ThreadDict | None:
        tid = UUID(thread_id)
        stored = await self._threads.get(tid)
        if stored is None:
            return None

        user_identifier = await self._identifier_of(stored.user_id)
        feedback_rows = await self._feedbacks.list_of_thread(tid)
        element_rows = await self._elements.list_of_thread(tid)

        steps = list(await self._feed.steps(thread_id, user_identifier))
        feedback_by_step = {
            Codec.uuid_str(f.for_id): FeedbackDicts.dict_of(f) for f in feedback_rows
        }
        for step in steps:
            step[StepField.FEEDBACK] = feedback_by_step.get(step.get(StepField.ID, ""))

        elements: list[ElementDict] = [ElementDicts.dict_of(e) for e in element_rows]

        thread = ThreadDicts.thread(stored, user_identifier, steps, elements)
        self._sign_element_urls(thread)

        return thread

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
        incoming = metadata or {}
        meta_set = {k: v for k, v in incoming.items() if v is not None}
        meta_del = [k for k, v in incoming.items() if v is None]
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
        tid = UUID(thread_id)
        await self._feedbacks.delete_of_thread(tid)
        await self._elements.delete_of_thread(tid)
        owner = await self._threads.delete(tid)

        self._purge_stream_journal(owner, thread_id)
        if owner is not None:
            await self._thread_changed(owner, thread_id, "", ChangeAction.DELETED)

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

    @staticmethod
    def _purge_stream_journal(owner: UUID | None, thread_id: str) -> None:
        """Журналы вывода инструментов умирают вместе с тредом.

        Сбой уборки не отменяет удаление треда — журнал доберёт ротация.
        """
        if owner is None:
            return

        journal = StreamJournalHub.get()
        if journal is None:
            return

        try:
            journal.purge_thread(str(owner), thread_id)
        except StreamJournalError:
            logger.warning(
                "stream journal purge failed for thread %s",
                thread_id,
                exc_info=True,
            )

    @data_boundary
    async def list_threads(
        self,
        pagination: Pagination,
        filters: ThreadFilter,
    ) -> PaginatedResponse[ThreadDict]:
        if not filters.userId:
            raise DataRejectedError("list_threads", "userId is required")

        user_id = UUID(filters.userId)
        user_identifier = await self._identifier_of(user_id)
        rows = await self._threads.list_of(user_id, pagination.first + 1)

        has_next = len(rows) > pagination.first
        page = [
            ThreadDicts.thread(t, user_identifier, [], [])
            for t in rows[: pagination.first]
        ]

        start_cursor = None
        end_cursor = None

        if page:
            start_cursor = page[0][ThreadField.ID]
            end_cursor = page[-1][ThreadField.ID]

        return PaginatedResponse(
            pageInfo=PageInfo(
                hasNextPage=has_next,
                startCursor=start_cursor,
                endCursor=end_cursor,
            ),
            data=page,
        )

    @data_boundary
    async def build_debug_url(self) -> str:
        return ""

    @data_boundary
    async def close(self) -> None:
        await self._storage.close()

    def _session_user_id(self) -> str:
        """Владелец файлов вложений — пользователь текущей сессии chainlit."""
        user_id = self._sessions.current().user_id
        if user_id is None:
            raise DataBrokenError("_session_user_id", "no chainlit session")

        return str(user_id)

    def _sign_element_urls(self, thread: ThreadDict) -> None:
        for element in thread.get(ThreadField.ELEMENTS) or []:
            self._sign_element_url(element)

    def _sign_element_url(self, element: ElementDict) -> None:
        """Собирает ссылку на вложение: она вычисляется, а не хранится."""
        props = ElementProps.of(element.get(ElementField.PROPS))
        element[ElementField.URL] = self._links.url(
            element.get(ElementField.THREAD_ID),
            element.get(ElementField.ID),
            props.dir,
        )
