"""PostgresDataLayer chainlit поверх psycopg AsyncConnectionPool.

Пул соединений приходит извне (владеет им DI), слой его не создаёт и не закрывает.
Все таблицы квалифицируются схемой из конфига (sql.Identifier(schema, ...)) —
на search_path соединения не опираемся. Каждый внешний контракт — один атомарный
SQL: запись через ON CONFLICT/CTE, чтение тредов целиком собирается в SQL
(jsonb_agg). Строки одиночных моделей мапятся через class_row, jsonb-поля на
запись оборачиваются в Jsonb (Row.params()). Контракт совпадает с BaseDataLayer.
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
from psycopg_pool import AsyncConnectionPool

from boba.chainlit2.chat.data.models import (
    Codec,
    Element,
    Feedback,
    Step,
    Thread,
    User,
)
from boba.chainlit2.errors import InternalServiceError

__all__ = [
    "PostgresDataLayer",
]


class PostgresDataLayer(BaseDataLayer):
    """Хранилище chainlit (users/threads/steps/elements/feedbacks) на psycopg-пуле."""

    # модели, чьи таблицы создаёт setup() (DDL живёт в самой модели — Row.ddl)
    _MODELS: ClassVar[tuple[type, ...]] = (User, Thread, Step, Element, Feedback)

    def __init__(
        self,
        pool: AsyncConnectionPool,
        schema: str,
        storage: BaseStorageClient,
    ) -> None:
        self._pool = pool
        self._schema = schema
        self._storage = storage

    def _table(self, name: str) -> sql.Identifier:
        """Квалифицированный идентификатор таблицы: \"schema\".\"name\"."""
        return sql.Identifier(self._schema, name)

    async def setup(self) -> None:
        """Создаёт схему и таблицы/индексы моделей"""
        async with self._pool.connection() as conn:
            try:
                async with conn.transaction():
                    await conn.execute(
                        sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                            sql.Identifier(self._schema)
                        )
                    )
            except InsufficientPrivilege:
                # схему уже создал админ, а прав на CREATE нет — продолжаем
                logger.info(
                    f"нет прав на CREATE SCHEMA {self._schema!r}, "
                    "считаем что её создал администратор"
                )
            for model in self._MODELS:
                async with conn.transaction():
                    for stmt in model.ddl(self._schema):
                        await conn.execute(stmt)

    async def get_user(self, identifier: str) -> PersistedUser | None:
        """
        Найти пользователя по identifier
        """
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
        """
        Создать пользователя или обновить его при повторном входе (get-or-create).

        Идемпотентный upsert по уникальному identifier
        при конфликте меняется только meta, id/created_at сохраняются
        Зовётся на каждом логине
        """
        # ON CONFLICT только meta
        # RETURNING отдаёт итоговую строку
        model = User.from_chainlit(user)
        query = sql.SQL(
            """
            insert into {users} ({cols})
            values ({ph})
            on conflict (identifier)
            do update set
                meta = excluded.meta
            returning {cols}
            """
        ).format(
            users=User.get_table_name(self._schema),
            cols=User.all_columns(),
            ph=User.all_placeholders(),
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
        """
        insert/update step feedback
        вернуть id оценки строкой
        """
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
        """Удалить оценку по id; вернуть True.

        Идемпотентно: отсутствие строки не ошибка. bool — индикатор успеха фронту.
        """
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
        """Содержимое элемента из content/path (для загрузки в storage)."""
        if element.content is not None:
            return element.content

        if element.path:
            async with aiofiles.open(element.path, "rb") as f:
                return await f.read()

        return None

    @queue_until_user_message()
    async def create_element(self, element: ChainlitElement) -> None:
        """
        Сохранить вложение: залить контент в storage и записать строку elements
        """
        if not element.for_id:
            return

        content = await self._read_element_content(element)
        if content is None:
            raise ValueError("Content is None, cannot upload file")

        user_id = await self._user_id_by_thread(element.thread_id)
        if user_id is None:
            # в теории не должно быть
            user_id = "unknown"

        if element.name:
            object_key = f"{user_id}/{element.id}/{element.name}"
        else:
            object_key = f"{user_id}/{element.id}"

        mime = element.mime or "application/octet-stream"
        uploaded = await self._storage.upload_file(
            object_key=object_key,
            data=content,
            mime=mime,
            overwrite=True,
        )

        if not uploaded:
            raise ValueError("Failed to upload file to storage")

        data = element.to_dict()
        data["url"] = uploaded.get("url")
        data["objectKey"] = uploaded.get("object_key")
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
        try:
            async with self._pool.connection() as conn:
                await conn.execute(query, model.all_params())
        except Exception as e:
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.create_element failed with error: {e}"
                ),
                user_detail="Not able to create element",
            ) from e

    async def get_element(self, thread_id: str, element_id: str) -> ElementDict | None:
        """Вернуть элемент треда по (thread_id, element_id) как ElementDict или None."""
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

        return row.to_chainlit() if row else None

    @queue_until_user_message()
    async def delete_element(
        self, element_id: str, thread_id: str | None = None
    ) -> None:
        """
        Удалить элемент по id и подчистить его файл в storage
        """
        query = sql.SQL(
            "delete from {table} where id = %s returning object_key"
        ).format(table=Element.get_table_name(self._schema))

        try:
            async with (
                self._pool.connection() as conn,
                conn.cursor(row_factory=tuple_row) as cur,
            ):
                await cur.execute(query, (UUID(element_id),))
                row = await cur.fetchone()
                if row and row[0]:
                    await self._storage.delete_file(object_key=row[0])
        except Exception as e:
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.delete_element failed with error: {e}"
                ),
                user_detail="Not able to delete element",
            ) from e

    @queue_until_user_message()
    async def create_step(self, step_dict: StepDict) -> None:
        """
        Создать/обновить шаг треда (upsert по id)
        """
        model = Step.from_chainlit(step_dict)
        thread_query = sql.SQL(
            """
            insert into {table} (id, created_at, meta)
            values (%(thread_id)s, %(created_at)s, '{{}}'::jsonb)
            on conflict (id) do nothing
            """
        ).format(
            table=Thread.get_table_name(self._schema),
        )
        step_query = sql.SQL(
            """
            insert into {steps} ({cols}) values ({ph})
            on conflict (id) do update set {asg}
            """
        ).format(
            steps=Step.get_table_name(self._schema),
            cols=Step.all_columns(),
            ph=Step.all_placeholders(),
            asg=Step.all_assignments(exclude=("id",)),
        )
        params = model.all_params()
        try:
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute(thread_query, params)
                await conn.execute(step_query, params)
        except Exception as e:
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.create_step failed with error: {e}"
                ),
                user_detail="Not able to create step",
            ) from e

    @queue_until_user_message()
    async def update_step(self, step_dict: StepDict) -> None:
        """Обновить шаг"""
        await self.create_step(step_dict)

    @queue_until_user_message()
    async def delete_step(self, step_id: str) -> None:
        """
        Удалить шаг и всё связанное: его feedbacks/elements (for_id), затем шаг.
        """
        feedbacks_query = sql.SQL("delete from {feedbacks} where for_id = %s").format(
            feedbacks=Feedback.get_table_name(self._schema)
        )
        elements_query = sql.SQL("delete from {elements} where for_id = %s").format(
            elements=Element.get_table_name(self._schema)
        )
        steps_query = sql.SQL("delete from {steps} where id = %s").format(
            steps=Step.get_table_name(self._schema)
        )
        params = (UUID(step_id),)
        try:
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute(feedbacks_query, params)
                await conn.execute(elements_query, params)
                await conn.execute(steps_query, params)
        except Exception as e:
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.delete_step failed with error: {e}"
                ),
                user_detail="Not able to delete step",
            ) from e

    async def get_favorite_steps(self, user_id: str) -> list[StepDict]:
        """Вернуть избранные шаги пользователя (meta.favorite == true), новые первыми"""
        query = sql.SQL(
            """
            select
                {cols}
            from
                {steps} s
                inner join {threads} t on s.thread_id = t.id
            where 1=1
                and t.user_id = %(user_id)s
                and s.meta @> '{{\"favorite\": true}}'::jsonb
            order by
                s.created_at desc
            """
        ).format(
            cols=Step.all_columns(prefix="s"),
            steps=Step.get_table_name(self._schema),
            threads=Thread.get_table_name(self._schema),
        )

        try:
            async with (
                self._pool.connection() as conn,
                conn.cursor(row_factory=class_row(Step)) as cur,
            ):
                await cur.execute(query, {"user_id": UUID(user_id)})
                rows = await cur.fetchall()
        except Exception as e:
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}.get_favorite_steps "
                    f"failed with error: {e}"
                ),
                user_detail="Not able to get favorite steps",
            ) from e

        res = [s.to_chainlit() for s in rows]
        return res

    async def get_thread_author(self, thread_id: str) -> str:
        """
        Вернуть identifier владельца треда
        """
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
        """
        Вернуть тред целиком отдельными запросами (тред / шаги+feedback /
        elements) и собрать ThreadDict в Python через Model.to_chainlit().
        """
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
        steps_query = sql.SQL(
            """
            select
                {cols}
            from
                {steps}
            where
                thread_id = %s
            order by
                created_at asc
            """
        ).format(
            cols=Step.all_columns(),
            steps=Step.get_table_name(self._schema),
        )
        feedbacks_query = sql.SQL(
            """
            select
                {cols}
            from
                {feedbacks} f
                inner join {steps} s on f.for_id = s.id
            where
                s.thread_id = %s
            """
        ).format(
            cols=Feedback.all_columns(prefix="f"),
            feedbacks=Feedback.get_table_name(self._schema),
            steps=Step.get_table_name(self._schema),
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

                async with conn.cursor(row_factory=class_row(Step)) as cur:
                    await cur.execute(steps_query, (tid,))
                    step_rows = await cur.fetchall()

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

        feedback_by_step = {f.for_id: f.to_chainlit() for f in feedback_rows}
        steps: list[StepDict] = []
        for s in step_rows:
            step = s.to_chainlit()
            step["feedback"] = feedback_by_step.get(s.id)
            steps.append(step)

        elements: list[ElementDict] = [e.to_chainlit() for e in element_rows]

        thread = thread_row.to_chainlit(
            user_identifier=user_identifier,
            steps=steps,
            elements=elements,
        )
        await self._sign_element_urls(thread)

        return thread

    async def update_thread(
        self,
        thread_id: str,
        name: str | None = None,
        user_id: str | None = None,
        metadata: dict | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """
        Создать тред или обновить переданные поля (upsert по id).
        """
        incoming = metadata or {}
        meta_set = {k: v for k, v in incoming.items() if v is not None}
        meta_del = [k for k, v in incoming.items() if v is None]
        name_value = name if name is not None else meta_set.get("name")

        params = {
            "id": UUID(thread_id),
            "created_at": Codec.now(),
            "name": name_value,
            "user_id": Codec.uuid_opt(user_id),
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
        """
        Удалить тред и всё содержимое: feedbacks, steps, elements и сам тред.
        """
        feedbacks_query = sql.SQL(
            """
            delete from {feedbacks}
            where thread_id = %(tid)s
               or for_id in (select id from {steps} where thread_id = %(tid)s)
            """
        ).format(
            feedbacks=Feedback.get_table_name(self._schema),
            steps=Step.get_table_name(self._schema),
        )
        elements_query = sql.SQL(
            "delete from {elements} where thread_id = %(tid)s"
        ).format(elements=Element.get_table_name(self._schema))
        steps_query = sql.SQL("delete from {steps} where thread_id = %(tid)s").format(
            steps=Step.get_table_name(self._schema)
        )
        thread_query = sql.SQL("delete from {threads} where id = %(tid)s").format(
            threads=Thread.get_table_name(self._schema)
        )
        params = {"tid": UUID(thread_id)}
        try:
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute(feedbacks_query, params)
                await conn.execute(elements_query, params)
                await conn.execute(steps_query, params)
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
        """
        список тредов пользователя
        """
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

        user_id = UUID(filters.userId)
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
        """URL треда во внешней debug-системе; здесь не используется — пустая строка."""
        return ""

    async def close(self) -> None:
        """Освободить ресурсы слоя.

        Пул принадлежит DI и закрывается извне — его не трогаем; закрываем storage.
        """
        await self._storage.close()

    async def _user_id_by_thread(self, thread_id: str) -> str | None:
        query = sql.SQL("select user_id from {table} where id = %s").format(
            table=Thread.get_table_name(self._schema)
        )

        try:
            async with (
                self._pool.connection() as conn,
                conn.cursor(row_factory=tuple_row) as cur,
            ):
                await cur.execute(query, (UUID(thread_id),))
                row = await cur.fetchone()
        except Exception as e:
            raise InternalServiceError(
                internal_detail=(
                    f"{type(self).__qualname__}._user_id_by_thread "
                    f"failed with error: {e}"
                ),
                user_detail="Not able to get user_id by thread",
            ) from e

        if row is None:
            return None

        user_id = str(row[0])

        return user_id

    async def _sign_element_urls(self, thread: ThreadDict) -> None:
        """Переписывает url элементов на свежий storage read-url."""
        for element in thread.get("elements") or []:
            if object_key := element.get("objectKey"):
                try:
                    element["url"] = await self._storage.get_read_url(object_key)
                except Exception as e:
                    logger.warning(
                        f"read-url для object_key '{object_key}' не получен: {e}"
                    )
