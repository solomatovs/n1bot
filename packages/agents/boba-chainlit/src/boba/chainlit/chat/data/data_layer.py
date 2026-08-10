"""PostgresDataLayer chainlit: оболочка диалога, сообщения хранит checkpointer."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar
from uuid import UUID

import aiofiles
import aiofiles.os
from psycopg import sql
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import class_row, tuple_row
from psycopg.types.json import Jsonb

from boba.chainlit.auth.errors import InternalServiceError
from boba.chainlit.chat.data.fields import ElementField, StepField, ThreadField
from boba.chainlit.chat.data.models import (
    Codec,
    Element,
    Feedback,
    Row,
    Thread,
    User,
)
from boba.chainlit.chat.data.object_key import (
    AttachmentLinks,
    ElementProps,
    ObjectKey,
)
from boba.chainlit.chat.data.storage import StorageClient
from boba.chainlit.chat.data.stream_journal import (
    StreamJournalError,
    StreamJournalHub,
)
from boba.chainlit.chat.errors import show_error
from boba.chainlit.chat.transcript import ConversationTranscript, ThreadMessages
from boba.chainlit.infra.session import current_user_id
from boba.chainlit.rendering.chat_view import ChatView, RecordingSink
from boba.db.postgres import AsyncPostgresPool
from chainlit.data.base import BaseDataLayer
from chainlit.data.utils import queue_until_user_message
from chainlit.element import Element as ChainlitElement
from chainlit.element import ElementDict
from chainlit.logger import logger
from chainlit.step import StepDict
from chainlit.types import (
    Feedback as FeedbackPayload,
)
from chainlit.types import (
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


class AttachmentDataLayer(BaseDataLayer, ABC):
    """Data layer, умеющий адресовать вложения публичными ссылками."""

    @property
    @abstractmethod
    def links(self) -> AttachmentLinks: ...

    @property
    @abstractmethod
    def storage(self) -> StorageClient: ...


class PostgresDataLayer(AttachmentDataLayer):
    """Хранилище chainlit (users/threads/elements/feedbacks) на psycopg-пуле."""

    _MODELS: ClassVar[tuple[type[Row], ...]] = (User, Thread, Element, Feedback)

    def __init__(
        self,
        pool: AsyncPostgresPool,
        schema: str,
        storage: StorageClient,
        messages: ThreadMessages,
        links: AttachmentLinks,
    ) -> None:
        self._pool = pool
        self._schema = schema
        self._storage = storage
        self._messages = messages
        self._links = links

    @property
    def links(self) -> AttachmentLinks:
        return self._links

    @property
    def storage(self) -> StorageClient:
        return self._storage

    async def _transcript_steps(
        self,
        thread_id: str,
        user_identifier: str | None,
    ) -> list[StepDict]:
        messages = await self._messages.load(thread_id)
        if not messages:
            return []

        sink = RecordingSink()
        view = ChatView(thread_id, sink, user_name=user_identifier)
        await ConversationTranscript(messages, view).replay()
        return sink.steps

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            try:
                async with conn.transaction():
                    await conn.execute(
                        sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                            sql.Identifier(self._schema)
                        )
                    )
            except InsufficientPrivilege:
                logger.info(
                    f"no permission for CREATE SCHEMA {self._schema!r}, "
                    "assuming an administrator created it"
                )
            for model in self._MODELS:
                async with conn.transaction():
                    for stmt in model.ddl(self._schema):
                        await conn.execute(stmt)

    async def get_user(self, identifier: str) -> PersistedUser | None:
        query = sql.SQL(
            """
            select
                {cols}
            from
                {users}
            where
                identifier = %s
            limit
                1
            """
        ).format(
            cols=User.all_columns(),
            users=User.get_table_name(self._schema),
        )

        try:
            async with (
                self._pool.connection() as conn,
                conn.cursor(row_factory=class_row(User)) as cur,
            ):
                await cur.execute(query, (identifier,))
                row = await cur.fetchone()
        except Exception as e:
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.get_user failed with error: {e}"
                ),
                user_detail="Not able to get user",
            ) from e

        if row is None:
            return None
        return row.to_persisted()

    async def create_user(self, user: ChainlitUser) -> PersistedUser | None:
        model = User.from_chainlit(user)
        query = sql.SQL(
            """
            insert into {users} ({insert_cols})
            values ({ph})
            on conflict (identifier)
            do update set
                meta = excluded.meta
            returning {cols}
            """
        ).format(
            users=User.get_table_name(self._schema),
            insert_cols=User.insert_columns(),
            ph=User.insert_placeholders(),
            cols=User.all_columns(),
        )
        try:
            async with (
                self._pool.connection() as conn,
                conn.cursor(row_factory=class_row(User)) as cur,
            ):
                await cur.execute(query, model.all_params())
                row = await cur.fetchone()
        except Exception as e:
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.create_user failed with error: {e}"
                ),
                user_detail="Not able to create user",
            ) from e

        if row is None:
            return None

        return row.to_persisted()

    async def upsert_feedback(self, feedback: FeedbackPayload) -> str:
        model = Feedback.from_payload(feedback)
        query = sql.SQL(
            """
            insert into {feedbacks} ({cols})
            values ({ph})
            on conflict (id)
            do update set
                {asg}
            """
        ).format(
            feedbacks=Feedback.get_table_name(self._schema),
            cols=Feedback.all_columns(),
            ph=Feedback.all_placeholders(),
            asg=Feedback.all_assignments(exclude=("id",)),
        )
        try:
            async with self._pool.connection() as conn:
                await conn.execute(query, model.all_params())
        except Exception as e:
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.upsert_feedback failed with error: {e}"
                ),
                user_detail="Not able to update feedback",
            ) from e

        return Codec.uuid_str(model.id)

    async def delete_feedback(self, feedback_id: str) -> bool:
        query = sql.SQL("""delete from {feedbacks} where id = %(id)s""").format(
            feedbacks=Feedback.get_table_name(self._schema)
        )
        try:
            async with self._pool.connection() as conn:
                await conn.execute(query, {"id": UUID(feedback_id)})
        except Exception as e:
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.delete_feedback failed with error: {e}"
                ),
                user_detail="Not able to delete feedback",
            ) from e

        return True

    async def _store_element_body(
        self, element: ChainlitElement, object_key: str, mime: str
    ) -> None:
        """content уходит как есть, файл с диска — потоком; нет ни того ни
        другого — содержимое уже залил в хранилище стриминговый роут."""
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
    def _require_uploaded(uploaded: Mapping[str, object]) -> None:
        if not uploaded:
            msg = "Failed to upload file to storage"
            raise ValueError(msg)

    @queue_until_user_message()
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
            await show_error(f"Failed to save attachment {element.name!r}: {e}")
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.create_element failed with error: {e}"
                ),
                user_detail="Not able to create element",
            ) from e

    async def _create_element(self, element: ChainlitElement) -> None:
        user_id = self._session_user_id()

        mime = element.mime or "application/octet-stream"

        data = element.to_dict()
        data[ElementField.MIME] = mime

        model = Element.from_chainlit(data)
        query = sql.SQL(
            """
            insert into {elements} ({cols})
            values ({ph})
            on conflict (id)
            do update set
                {asg}
            """
        ).format(
            elements=Element.get_table_name(self._schema),
            cols=Element.all_columns(),
            ph=Element.all_placeholders(),
            asg=Element.all_assignments(exclude=("id",)),
        )
        object_key = ObjectKey.build(
            user_id, element.thread_id, element.name, element.id
        ).render()
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(query, model.all_params())
            await self._store_element_body(element, object_key, mime)

    async def get_element(self, thread_id: str, element_id: str) -> ElementDict | None:
        query = sql.SQL(
            """
            select
                {cols}
            from
                {elements}
            where 1=1
                and thread_id = %(thread_id)s
                and id = %(id)s
            """
        ).format(
            cols=Element.all_columns(),
            elements=Element.get_table_name(self._schema),
        )

        try:
            async with (
                self._pool.connection() as conn,
                conn.cursor(row_factory=class_row(Element)) as cur,
            ):
                await cur.execute(
                    query, {"thread_id": UUID(thread_id), "id": UUID(element_id)}
                )
                row = await cur.fetchone()
        except Exception as e:
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.get_element failed with error: {e}"
                ),
                user_detail="Not able to get element",
            ) from e

        if row is None:
            return None

        element = row.to_chainlit()
        self._sign_element_url(element)
        return element

    @queue_until_user_message()
    async def delete_element(
        self, element_id: str, thread_id: str | None = None
    ) -> None:
        query = sql.SQL(
            "delete from {table} where id = %s returning thread_id, name"
        ).format(table=Element.get_table_name(self._schema))

        try:
            async with (
                self._pool.connection() as conn,
                conn.transaction(),
                conn.cursor(row_factory=tuple_row) as cur,
            ):
                await cur.execute(query, (UUID(element_id),))
                row = await cur.fetchone()
                if row and row[0]:
                    user_id = self._session_user_id()
                    await self._storage.delete_file(
                        object_key=ObjectKey.build(
                            user_id, row[0], row[1], element_id
                        ).render(),
                    )
        except Exception as e:
            # вызывается фоновой таской chainlit: raise до пользователя не дойдёт
            await show_error(f"Failed to delete attachment {element_id}: {e}")
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.delete_element failed with error: {e}"
                ),
                user_detail="Not able to delete element",
            ) from e

    async def create_step(self, step_dict: StepDict) -> None:
        pass

    async def update_step(self, step_dict: StepDict) -> None:
        pass

    @queue_until_user_message()
    async def delete_step(self, step_id: str) -> None:
        feedbacks_query = sql.SQL("delete from {feedbacks} where for_id = %s").format(
            feedbacks=Feedback.get_table_name(self._schema)
        )
        elements_query = sql.SQL("delete from {elements} where for_id = %s").format(
            elements=Element.get_table_name(self._schema)
        )
        params = (UUID(step_id),)
        try:
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute(feedbacks_query, params)
                await conn.execute(elements_query, params)
        except Exception as e:
            # вызывается фоновой таской chainlit: raise до пользователя не дойдёт
            await show_error(f"Failed to delete message {step_id}: {e}")
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.delete_step failed with error: {e}"
                ),
                user_detail="Not able to delete step",
            ) from e

    async def get_favorite_steps(self, user_id: str) -> list[StepDict]:
        return []

    async def get_thread_author(self, thread_id: str) -> str:
        query = sql.SQL(
            """
            select
                u.identifier
            from
                {threads} t
                inner join {users} u on t.user_id = u.id
            where
                t.id = %(id)s
            """
        ).format(
            threads=Thread.get_table_name(self._schema),
            users=User.get_table_name(self._schema),
        )
        try:
            async with (
                self._pool.connection() as conn,
                conn.cursor(row_factory=tuple_row) as cur,
            ):
                await cur.execute(query, {"id": UUID(thread_id)})
                row = await cur.fetchone()
        except Exception as e:
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.get_thread_author "
                    f"failed with error: {e}"
                ),
                user_detail="Not able to get thread author",
            ) from e

        if row and row[0] is not None:
            return row[0]

        raise ValueError(f"Author not found for thread_id {thread_id}")

    async def get_thread(self, thread_id: str) -> ThreadDict | None:
        thread_query = sql.SQL(
            """
            select
                {cols}
            from
                {threads}
            where
                id = %s
            """
        ).format(
            cols=Thread.all_columns(),
            threads=Thread.get_table_name(self._schema),
        )
        feedbacks_query = sql.SQL(
            """
            select
                {cols}
            from
                {feedbacks}
            where
                thread_id = %s
            """
        ).format(
            cols=Feedback.all_columns(),
            feedbacks=Feedback.get_table_name(self._schema),
        )
        elements_query = sql.SQL(
            """
            select
                {cols}
            from
                {elements}
            where
                thread_id = %s
            """
        ).format(
            cols=Element.all_columns(),
            elements=Element.get_table_name(self._schema),
        )
        identifier_query = sql.SQL(
            "select identifier from {users} where id = %s"
        ).format(users=User.get_table_name(self._schema))

        try:
            async with self._pool.connection() as conn, conn.transaction():
                tid = UUID(thread_id)

                async with conn.cursor(row_factory=class_row(Thread)) as cur:
                    await cur.execute(thread_query, (tid,))
                    thread_row = await cur.fetchone()

                if thread_row is None:
                    return None

                user_identifier: str | None = None
                if thread_row.user_id is not None:
                    async with conn.cursor(row_factory=tuple_row) as cur:
                        await cur.execute(identifier_query, (thread_row.user_id,))
                        identifier_row = await cur.fetchone()
                        if identifier_row is not None:
                            user_identifier = identifier_row[0]

                async with conn.cursor(row_factory=class_row(Feedback)) as cur:
                    await cur.execute(feedbacks_query, (tid,))
                    feedback_rows = await cur.fetchall()

                async with conn.cursor(row_factory=class_row(Element)) as cur:
                    await cur.execute(elements_query, (tid,))
                    element_rows = await cur.fetchall()
        except Exception as e:
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.get_thread failed with error: {e}"
                ),
                user_detail="Not able to get thread",
            ) from e

        steps = await self._transcript_steps(thread_id, user_identifier)
        feedback_by_step = {
            Codec.uuid_str(f.for_id): f.to_chainlit() for f in feedback_rows
        }
        for step in steps:
            step[StepField.FEEDBACK] = feedback_by_step.get(step.get(StepField.ID, ""))

        elements: list[ElementDict] = [e.to_chainlit() for e in element_rows]

        thread = thread_row.to_chainlit(
            user_identifier=user_identifier,
            steps=steps,
            elements=elements,
        )
        self._sign_element_urls(thread)

        return thread

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

        user_id_value: int | None = None
        if user_id:
            user_id_value = int(user_id)

        params = {
            "id": UUID(thread_id),
            "created_at": Codec.now(),
            "name": name_value,
            "user_id": user_id_value,
            "tags": tags,
            "meta_set": Jsonb(meta_set),
            "meta_del": meta_del,
        }
        query = sql.SQL(
            """
            insert into {threads} as t (
                id,
                created_at,
                name,
                user_id,
                tags,
                meta
            )
            values (
                %(id)s,
                %(created_at)s,
                %(name)s,
                %(user_id)s,
                %(tags)s,
                %(meta_set)s
            )
            on conflict (id) do update set
                name             = COALESCE(excluded.name,              t.name),
                user_id          = COALESCE(excluded.user_id,           t.user_id),
                tags             = COALESCE(excluded.tags,              t.tags),
                meta             = (COALESCE(t.meta, '{{}}'::jsonb) - %(meta_del)s::text[]) || %(meta_set)s::jsonb
            """  # noqa: E501
        ).format(
            threads=Thread.get_table_name(self._schema),
        )
        try:
            async with self._pool.connection() as conn:
                await conn.execute(query, params)
        except Exception as e:
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.update_thread failed with error: {e}"
                ),
                user_detail="Not able to update thread",
            ) from e

    async def delete_thread(self, thread_id: str) -> None:
        feedbacks_query = sql.SQL(
            "delete from {feedbacks} where thread_id = %(tid)s"
        ).format(feedbacks=Feedback.get_table_name(self._schema))
        elements_query = sql.SQL(
            "delete from {elements} where thread_id = %(tid)s"
        ).format(elements=Element.get_table_name(self._schema))
        thread_query = sql.SQL(
            "delete from {threads} where id = %(tid)s returning user_id"
        ).format(threads=Thread.get_table_name(self._schema))
        params = {"tid": UUID(thread_id)}
        try:
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute(feedbacks_query, params)
                await conn.execute(elements_query, params)
                cursor = await conn.execute(thread_query, params)
                owner = await cursor.fetchone()
        except Exception as e:
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.delete_thread failed with error: {e}"
                ),
                user_detail="Not able to delete thread",
            ) from e

        self._purge_stream_journal(owner, thread_id)

    @staticmethod
    def _purge_stream_journal(owner: tuple[Any, ...] | None, thread_id: str) -> None:
        """Журналы вывода инструментов умирают вместе с тредом.

        Сбой уборки не отменяет удаление треда — журнал доберёт ротация.
        """
        if owner is None or owner[0] is None:
            return

        journal = StreamJournalHub.get()
        if journal is None:
            return

        try:
            journal.purge_thread(str(owner[0]), thread_id)
        except StreamJournalError:
            logger.warning(
                "stream journal purge failed for thread %s", thread_id,
                exc_info=True,
            )

    async def list_threads(
        self,
        pagination: Pagination,
        filters: ThreadFilter,
    ) -> PaginatedResponse[ThreadDict]:
        if not filters.userId:
            raise ValueError("userId is required")

        query = sql.SQL(
            """
            select
                {cols_t}
            from
                {threads}
            where
                user_id = %(user_id)s
            order by
                created_at desc
            limit
                %(limit)s
            """
        ).format(
            cols_t=Thread.all_columns(),
            threads=Thread.get_table_name(self._schema),
        )
        identifier_query = sql.SQL(
            "select identifier from {users} where id = %(user_id)s"
        ).format(users=User.get_table_name(self._schema))

        user_id = int(filters.userId)
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor(row_factory=tuple_row) as cur:
                    await cur.execute(identifier_query, {"user_id": user_id})
                    identifier_row = await cur.fetchone()

                async with conn.cursor(row_factory=class_row(Thread)) as cur:
                    await cur.execute(
                        query,
                        {"user_id": user_id, "limit": pagination.first + 1},
                    )
                    rows = await cur.fetchall()
        except Exception as e:
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.list_threads failed with error: {e}"
                ),
                user_detail="Not able to list threads",
            ) from e

        user_identifier = None
        if identifier_row is not None:
            user_identifier = identifier_row[0]
        has_next = len(rows) > pagination.first
        page = [
            t.to_chainlit(user_identifier=user_identifier, steps=[], elements=[])
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

    async def build_debug_url(self) -> str:
        return ""

    async def close(self) -> None:
        await self._storage.close()

    def _session_user_id(self) -> str:
        """Владелец файлов вложений — пользователь текущей сессии chainlit."""
        user_id = current_user_id()
        if user_id is None:
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}: no chainlit session, "
                    f"cannot build the attachment path"
                ),
                user_detail="Not able to resolve current user",
            )

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
