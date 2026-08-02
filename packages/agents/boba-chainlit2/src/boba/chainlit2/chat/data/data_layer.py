"""PostgresDataLayer chainlit поверх boba AsyncPostgresPool.

Слой хранит только оболочку диалога: пользователей, треды, вложения и оценки.
Сами сообщения живут в langgraph-checkpointer'е — get_thread собирает из них
ленту, а create_step/update_step ничего не пишут.

Пул соединений приходит извне (владеет им DI), слой его не создаёт и не закрывает.
Все таблицы квалифицируются схемой из конфига (sql.Identifier(schema, ...)) —
на search_path соединения не опираемся. Запись идёт через ON CONFLICT, строки
моделей мапятся через class_row, jsonb-поля оборачиваются в Jsonb (Row.params()).
Контракт совпадает с BaseDataLayer.
"""

from typing import ClassVar
from uuid import UUID

import aiofiles
from chainlit.data.base import BaseDataLayer
from chainlit.data.storage_clients.base import BaseStorageClient
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
from psycopg import sql
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import class_row, tuple_row
from psycopg.types.json import Jsonb

from boba.auth.errors import InternalServiceError
from boba.chainlit2.chat.data.models import (
    Codec,
    Element,
    Feedback,
    Row,
    Thread,
    User,
)
from boba.chainlit2.chat.handler.error import show_error
from boba.chainlit2.chat.transcript import ConversationTranscript, ThreadMessages
from boba.chainlit2.infra.session import current_user_id
from boba.chainlit2.rendering.chat_view import ChatView, RecordingSink
from boba.db.postgres import AsyncPostgresPool

__all__ = [
    "PostgresDataLayer",
]


class PostgresDataLayer(BaseDataLayer):
    """Хранилище chainlit (users/threads/elements/feedbacks) на psycopg-пуле."""

    _MODELS: ClassVar[tuple[type[Row], ...]] = (User, Thread, Element, Feedback)

    def __init__(
        self,
        pool: AsyncPostgresPool,
        schema: str,
        storage: BaseStorageClient,
        messages: ThreadMessages,
    ) -> None:
        self._pool = pool
        self._schema = schema
        self._storage = storage
        self._messages = messages

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

        return row.to_persisted() if row else None

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

    @staticmethod
    async def _read_element_content(element: ChainlitElement) -> bytes | str | None:
        if element.content is not None:
            return element.content

        if element.path:
            async with aiofiles.open(element.path, "rb") as f:
                return await f.read()

        return None

    @queue_until_user_message()
    async def create_element(self, element: ChainlitElement) -> None:
        if not element.for_id:
            logger.warning(
                "element %s without for_id is not persisted: "
                "it is not attached to anything",
                element.id,
            )
            return
        try:
            await self._create_element(element)
        except Exception as e:
            # chainlit зовёт create_element фоновой таской и гасит исключение:
            # без явного сообщения пользователь увидит «файл загружен»
            await show_error(f"Failed to save attachment {element.name!r}: {e}")
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.create_element failed with error: {e}"
                ),
                user_detail="Not able to create element",
            ) from e

    async def _create_element(self, element: ChainlitElement) -> None:
        content = await self._read_element_content(element)
        if content is None:
            raise ValueError("Content is None, cannot upload file")

        user_id = self._session_user_id()

        mime = element.mime or "application/octet-stream"

        data = element.to_dict()
        data["mime"] = mime

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
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(query, model.all_params())
            uploaded = await self._storage.upload_file(
                object_key=Element.object_key(user_id, element.thread_id, element.id),
                data=content,
                mime=mime,
                overwrite=True,
            )
            if not uploaded:
                msg = "Failed to upload file to storage"
                raise ValueError(msg)

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
        await self._sign_element_url(element, self._session_user_id())
        return element

    @queue_until_user_message()
    async def delete_element(
        self, element_id: str, thread_id: str | None = None
    ) -> None:
        query = sql.SQL(
            "delete from {table} where id = %s returning thread_id"
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
                        object_key=Element.object_key(user_id, row[0], element_id),
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
            step["feedback"] = feedback_by_step.get(step.get("id", ""))

        elements: list[ElementDict] = [e.to_chainlit() for e in element_rows]

        thread = thread_row.to_chainlit(
            user_identifier=user_identifier,
            steps=steps,
            elements=elements,
        )
        await self._sign_element_urls(thread, thread_row.user_id)

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
        name_value = name if name is not None else meta_set.get("name")

        params = {
            "id": UUID(thread_id),
            "created_at": Codec.now(),
            "name": name_value,
            "user_id": int(user_id) if user_id else None,
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
        thread_query = sql.SQL("delete from {threads} where id = %(tid)s").format(
            threads=Thread.get_table_name(self._schema)
        )
        params = {"tid": UUID(thread_id)}
        try:
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute(feedbacks_query, params)
                await conn.execute(elements_query, params)
                await conn.execute(thread_query, params)
        except Exception as e:
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.delete_thread failed with error: {e}"
                ),
                user_detail="Not able to delete thread",
            ) from e

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

        user_identifier = identifier_row[0] if identifier_row is not None else None
        has_next = len(rows) > pagination.first
        page = [
            t.to_chainlit(user_identifier=user_identifier, steps=[], elements=[])
            for t in rows[: pagination.first]
        ]
        return PaginatedResponse(
            pageInfo=PageInfo(
                hasNextPage=has_next,
                startCursor=page[0]["id"] if page else None,
                endCursor=page[-1]["id"] if page else None,
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

    async def _sign_element_urls(self, thread: ThreadDict, user_id: object) -> None:
        for element in thread.get("elements") or []:
            await self._sign_element_url(element, user_id)

    async def _sign_element_url(self, element: ElementDict, user_id: object) -> None:
        """Собирает ссылку на вложение: путь вычисляется, а не хранится."""
        object_key = Element.object_key(
            user_id, element.get("threadId"), element.get("id")
        )
        try:
            element["url"] = await self._storage.get_read_url(object_key)
        except Exception as e:
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}._sign_element_url failed for "
                    f"object_key={object_key!r} with error: {e}"
                ),
                user_detail="Not able to get attachment link",
            ) from e
