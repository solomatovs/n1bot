"""PostgresDataLayer chainlit: оболочка диалога, сообщения хранит checkpointer."""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Protocol
from uuid import UUID

import aiofiles
import aiofiles.os
from psycopg import sql
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import class_row, tuple_row
from psycopg.types.json import Jsonb

from boba.chainlit.data.errors import (
    DataBrokenError,
    DataRejectedError,
    DataUnavailableError,
    data_boundary,
)
from boba.chainlit.data.models import (
    Codec,
    Element,
    Feedback,
    Row,
    Thread,
    User,
)
from boba.chainlit.data.storage import StorageClient
from boba.chainlit.domain.fields import ElementField, StepField, ThreadField
from boba.chainlit.domain.keys import (
    AttachmentLinks,
    ElementProps,
    ObjectKey,
)
from boba.chainlit.domain.session import current_user_id
from boba.chainlit.domain.stream import StreamJournalError, StreamJournalHub
from boba.db.postgres import AsyncPostgresPool
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


class PostgresDataLayer(AttachmentDataLayer):
    """Хранилище chainlit (users/threads/elements/feedbacks) на psycopg-пуле."""

    _MODELS: ClassVar[tuple[type[Row], ...]] = (User, Thread, Element, Feedback)

    def __init__(
        self,
        pool: AsyncPostgresPool,
        schema: str,
        storage: StorageClient,
        feed: ThreadFeed,
        links: AttachmentLinks,
    ) -> None:
        self._pool = pool
        self._schema = schema
        self._storage = storage
        self._feed = feed
        self._links = links

    @property
    def links(self) -> AttachmentLinks:
        return self._links

    @property
    def storage(self) -> StorageClient:
        return self._storage

    @data_boundary
    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            try:
                async with conn.transaction():
                    await conn.execute(
                        sql.SQL("create schema if not exists {}").format(
                            sql.Identifier(self._schema)
                        )
                    )
            except InsufficientPrivilege:
                logger.info(
                    "no permission for CREATE SCHEMA %r, "
                    "assuming an administrator created it",
                    self._schema,
                )
            for model in self._MODELS:
                async with conn.transaction():
                    for stmt in model.ddl(self._schema):
                        await conn.execute(stmt)

    @data_boundary
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
            raise DataUnavailableError("get_user", str(e)) from e

        if row is None:
            return None
        return row.to_persisted()

    @data_boundary
    async def create_user(self, user: ChainlitUser) -> PersistedUser | None:
        model = User.from_chainlit(user)
        query = sql.SQL(
            """
            insert into {users} (
                {insert_cols}
            )
            values (
                {ph}
            )
            on conflict (identifier) do update set
                meta = excluded.meta
            returning
                {cols}
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
            raise DataUnavailableError("create_user", str(e)) from e

        if row is None:
            return None

        return row.to_persisted()

    @data_boundary
    async def upsert_feedback(self, feedback: FeedbackPayload) -> str:
        model = Feedback.from_payload(feedback)
        query = sql.SQL(
            """
            insert into {feedbacks} (
                {cols}
            )
            values (
                {ph}
            )
            on conflict (id) do update set
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
            raise DataUnavailableError("upsert_feedback", str(e)) from e

        return Codec.uuid_str(model.id)

    @data_boundary
    async def delete_feedback(self, feedback_id: str) -> bool:
        query = sql.SQL(
            """
            delete from
                {feedbacks}
            where
                id = %(id)s
            """
        ).format(
            feedbacks=Feedback.get_table_name(self._schema),
        )
        try:
            async with self._pool.connection() as conn:
                await conn.execute(query, {"id": UUID(feedback_id)})
        except Exception as e:
            raise DataUnavailableError("delete_feedback", str(e)) from e

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
        user_id = self._session_user_id()

        mime = element.mime or "application/octet-stream"

        data = element.to_dict()
        data[ElementField.MIME] = mime

        model = Element.from_chainlit(data)
        query = sql.SQL(
            """
            insert into {elements} (
                {cols}
            )
            values (
                {ph}
            )
            on conflict (id) do update set
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

    @data_boundary
    async def get_element(self, thread_id: str, element_id: str) -> ElementDict | None:
        query = sql.SQL(
            """
            select
                {cols}
            from
                {elements}
            where
                thread_id = %(thread_id)s
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
            raise DataUnavailableError("get_element", str(e)) from e

        if row is None:
            return None

        element = row.to_chainlit()
        self._sign_element_url(element)
        return element

    @queue_until_user_message()
    @data_boundary
    async def delete_element(
        self, element_id: str, thread_id: str | None = None
    ) -> None:
        query = sql.SQL(
            """
            delete from
                {table}
            where
                id = %s
            returning
                thread_id,
                name
            """
        ).format(
            table=Element.get_table_name(self._schema),
        )

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
            raise DataUnavailableError("delete_element", str(e)) from e

    @data_boundary
    async def create_step(self, step_dict: StepDict) -> None:
        pass

    @data_boundary
    async def update_step(self, step_dict: StepDict) -> None:
        pass

    @queue_until_user_message()
    @data_boundary
    async def delete_step(self, step_id: str) -> None:
        feedbacks_query = sql.SQL(
            """
            delete from
                {feedbacks}
            where
                for_id = %s
            """
        ).format(
            feedbacks=Feedback.get_table_name(self._schema),
        )
        elements_query = sql.SQL(
            """
            delete from
                {elements}
            where
                for_id = %s
            """
        ).format(
            elements=Element.get_table_name(self._schema),
        )
        params = (UUID(step_id),)
        try:
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute(feedbacks_query, params)
                await conn.execute(elements_query, params)
        except Exception as e:
            raise DataUnavailableError("delete_step", str(e)) from e

    @data_boundary
    async def get_favorite_steps(self, user_id: str) -> list[StepDict]:
        return []

    @data_boundary
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
            raise DataUnavailableError("get_thread_author", str(e)) from e

        if row and row[0] is not None:
            return row[0]

        raise DataRejectedError(
            "get_thread_author", f"no author for thread {thread_id}"
        )

    @data_boundary
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
            """
            select
                identifier
            from
                {users}
            where
                id = %s
            """
        ).format(
            users=User.get_table_name(self._schema),
        )

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
            raise DataUnavailableError("get_thread", str(e)) from e

        steps = list(await self._feed.steps(thread_id, user_identifier))
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
            raise DataUnavailableError("update_thread", str(e)) from e

    @data_boundary
    async def delete_thread(self, thread_id: str) -> None:
        feedbacks_query = sql.SQL(
            """
            delete from
                {feedbacks}
            where
                thread_id = %(tid)s
            """
        ).format(
            feedbacks=Feedback.get_table_name(self._schema),
        )
        elements_query = sql.SQL(
            """
            delete from
                {elements}
            where
                thread_id = %(tid)s
            """
        ).format(
            elements=Element.get_table_name(self._schema),
        )
        thread_query = sql.SQL(
            """
            delete from
                {threads}
            where
                id = %(tid)s
            returning
                user_id
            """
        ).format(
            threads=Thread.get_table_name(self._schema),
        )
        params = {"tid": UUID(thread_id)}
        try:
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute(feedbacks_query, params)
                await conn.execute(elements_query, params)
                cursor = await conn.execute(thread_query, params)
                owner = await cursor.fetchone()
        except Exception as e:
            raise DataUnavailableError("delete_thread", str(e)) from e

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
            """
            select
                identifier
            from
                {users}
            where
                id = %(user_id)s
            """
        ).format(
            users=User.get_table_name(self._schema),
        )

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
            raise DataUnavailableError("list_threads", str(e)) from e

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

    @data_boundary
    async def build_debug_url(self) -> str:
        return ""

    @data_boundary
    async def close(self) -> None:
        await self._storage.close()

    def _session_user_id(self) -> str:
        """Владелец файлов вложений — пользователь текущей сессии chainlit."""
        user_id = current_user_id()
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
