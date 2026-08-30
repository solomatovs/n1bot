"""Dataclass-модели chainlit-данных: users, threads, elements, feedbacks."""

from dataclasses import Field, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal, Self, TypeVar
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from boba.chainlit.domain.fields import ElementField
from boba.runtime.tables import ChatTable
from chainlit.element import (
    ElementDict,
    ElementDisplay,
    ElementSize,
    ElementType,
)
from chainlit.step import StepDict
from chainlit.types import (
    Feedback as FeedbackPayload,
)
from chainlit.types import (
    FeedbackDict,
    ThreadDict,
)
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser

__all__ = [
    "Codec",
    "Element",
    "Feedback",
    "Row",
    "Thread",
    "User",
]


_T = TypeVar("_T")


class Codec:
    """Кодеки значений поле модели ↔ chainlit-dict (UUID/datetime)."""

    @staticmethod
    def require(value: _T | None) -> _T:
        if value is None:
            raise ValueError("required chainlit field is missing")
        return value

    @staticmethod
    def uuid(value: str | None) -> UUID:
        return UUID(Codec.require(value))

    @staticmethod
    def uuid_opt(value: str | None) -> UUID | None:
        if value:
            return UUID(value)
        return None

    @staticmethod
    def uuid_str(value: UUID) -> str:
        return str(value)

    @staticmethod
    def uuid_str_opt(value: UUID | None) -> str | None:
        if value is not None:
            return str(value)
        return None

    @staticmethod
    def iso(value: datetime) -> str:
        return value.isoformat()

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)


class Row:
    """База dataclass-моделей: список колонок/плейсхолдеров из полей."""

    __slots__ = ()
    __dataclass_fields__: ClassVar[dict[str, Field[Any]]]

    @classmethod
    def all_columns(cls, prefix: str | None = None) -> sql.Composable:
        columns: list[sql.Composable] = []
        for f in fields(cls):
            if prefix:
                columns.append(sql.Identifier(prefix, f.name))
            else:
                columns.append(sql.Identifier(f.name))
        return sql.SQL(", ").join(columns)

    @classmethod
    def all_placeholders(cls) -> sql.Composable:
        return sql.SQL(", ").join(sql.Placeholder(f.name) for f in fields(cls))

    @classmethod
    def insert_columns(cls) -> sql.Composable:
        return sql.SQL(", ").join(sql.Identifier(f.name) for f in cls._insertable())

    @classmethod
    def insert_placeholders(cls) -> sql.Composable:
        return sql.SQL(", ").join(sql.Placeholder(f.name) for f in cls._insertable())

    @classmethod
    def _insertable(cls) -> list[Field[Any]]:
        return [f for f in fields(cls) if not f.metadata.get("db_generated")]

    @classmethod
    def all_assignments(cls, *, exclude: tuple[str, ...] = ()) -> sql.Composable:
        return sql.SQL(", ").join(
            sql.SQL("{0} = excluded.{0}").format(sql.Identifier(f.name))
            for f in fields(cls)
            if f.name not in exclude
        )

    def all_params(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            jsonb = f.metadata.get("jsonb") and value is not None
            if jsonb:
                out[f.name] = Jsonb(value)
            else:
                out[f.name] = value
        return out

    @classmethod
    def ddl(cls, schema: str) -> tuple[sql.Composed, ...]:
        raise NotImplementedError


@dataclass(slots=True)
class User(Row):
    """Пользователь chainlit."""

    identifier: str
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=Codec.now)
    meta: dict[str, Any] = field(default_factory=dict, metadata={"jsonb": True})

    @staticmethod
    def get_table_name(schema: str) -> sql.Identifier:
        return ChatTable.USERS.under(schema)

    @classmethod
    def from_chainlit(cls, user: ChainlitUser) -> Self:
        # копия metadata: строка правит своё поле, а не словарь вызывающего
        return cls(
            identifier=user.identifier,
            meta=dict(user.metadata),
        )

    def to_persisted(self) -> PersistedUser:
        return PersistedUser(
            id=str(self.id),
            identifier=self.identifier,
            createdAt=Codec.iso(self.created_at),
            metadata=dict(self.meta),
        )

    @classmethod
    def ddl(cls, schema: str) -> tuple[sql.Composed, ...]:
        return (
            sql.SQL(
                """
                create table if not exists {table} (
                    id         uuid primary key default gen_random_uuid(),
                    identifier text not null unique,
                    created_at timestamptz not null,
                    meta       jsonb not null default '{{}}'::jsonb
                )
                """
            ).format(table=cls.get_table_name(schema)),
            # регистр логина не заводит вторую личность даже если запись
            # придёт мимо UserLogin: инвариант держит база
            sql.SQL(
                """
                create unique index if not exists idx_users_identifier_lower
                    on {table} (lower(identifier))
                """
            ).format(table=cls.get_table_name(schema)),
        )


@dataclass(slots=True)
class Thread(Row):
    """Диалог (тред)."""

    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=Codec.now)
    name: str | None = None
    user_id: UUID | None = None
    tags: list[str] | None = None
    meta: dict[str, Any] | None = field(default=None, metadata={"jsonb": True})

    @staticmethod
    def get_table_name(schema: str) -> sql.Identifier:
        return ChatTable.THREADS.under(schema)

    def to_chainlit(
        self,
        user_identifier: str | None,
        steps: list[StepDict],
        elements: list[ElementDict],
    ) -> ThreadDict:
        return ThreadDict(
            id=Codec.uuid_str(self.id),
            createdAt=Codec.iso(self.created_at),
            name=self.name,
            userId=Codec.uuid_str_opt(self.user_id),
            userIdentifier=user_identifier,
            tags=self.tags,
            metadata=self.meta,
            steps=steps,
            elements=elements,
        )

    @classmethod
    def ddl(cls, schema: str) -> tuple[sql.Composed, ...]:
        table = cls.get_table_name(schema)
        return (
            sql.SQL(
                """
                create table if not exists {table} (
                    id         uuid primary key,
                    created_at timestamptz not null,
                    name       text,
                    user_id    uuid,
                    tags       text[],
                    meta       jsonb
                )
                """
            ).format(table=table),
            sql.SQL(
                """
                create index if not exists idx_threads_user_id
                    on {table} (user_id)
                """
            ).format(table=table),
        )


@dataclass(slots=True)
class Element(Row):
    """Вложение/артефакт, привязанный к треду (и опционально к шагу)."""

    name: str
    type: ElementType
    display: ElementDisplay
    id: UUID = field(default_factory=uuid4)
    thread_id: UUID | None = None
    for_id: UUID | None = None
    chainlit_key: str | None = None
    size: ElementSize | None = None
    language: str | None = None
    page: int | None = None
    props: dict[str, Any] | None = field(default=None, metadata={"jsonb": True})
    mime: str | None = None

    @staticmethod
    def get_table_name(schema: str) -> sql.Identifier:
        return ChatTable.ELEMENTS.under(schema)

    @classmethod
    def from_chainlit(cls, data: ElementDict) -> Self:
        return cls(
            id=Codec.uuid(data.get(ElementField.ID)),
            thread_id=Codec.uuid_opt(data.get(ElementField.THREAD_ID)),
            for_id=Codec.uuid_opt(data.get(ElementField.FOR_ID)),
            type=Codec.require(data.get(ElementField.TYPE)),
            chainlit_key=data.get(ElementField.CHAINLIT_KEY),
            name=Codec.require(data.get(ElementField.NAME)),
            display=Codec.require(data.get(ElementField.DISPLAY)),
            size=data.get(ElementField.SIZE),
            language=data.get(ElementField.LANGUAGE),
            page=data.get(ElementField.PAGE),
            props=data.get(ElementField.PROPS),
            mime=data.get(ElementField.MIME),
        )

    def to_chainlit(self) -> ElementDict:
        return {
            ElementField.ID: Codec.uuid_str(self.id),
            ElementField.THREAD_ID: Codec.uuid_str_opt(self.thread_id),
            ElementField.TYPE: self.type,
            ElementField.CHAINLIT_KEY: self.chainlit_key,
            ElementField.NAME: self.name,
            ElementField.DISPLAY: self.display,
            ElementField.SIZE: self.size,
            ElementField.LANGUAGE: self.language,
            ElementField.PAGE: self.page,
            ElementField.PROPS: self.props,
            ElementField.FOR_ID: Codec.uuid_str_opt(self.for_id),
            ElementField.MIME: self.mime,
        }

    @classmethod
    def ddl(cls, schema: str) -> tuple[sql.Composed, ...]:
        table = cls.get_table_name(schema)
        return (
            sql.SQL(
                """
                create table if not exists {table} (
                    id           uuid primary key,
                    name         text not null,
                    type         text not null,
                    display      text not null,
                    thread_id    uuid,
                    for_id       uuid,
                    chainlit_key text,
                    size         text,
                    language     text,
                    page         integer,
                    props        jsonb,
                    mime         text
                )
                """
            ).format(table=table),
            sql.SQL(
                """
                create index if not exists idx_elements_thread_id
                    on {table} (thread_id)
                """
            ).format(table=table),
        )


@dataclass(slots=True)
class Feedback(Row):
    """Оценка шага пользователем."""

    for_id: UUID
    value: Literal[0, 1]
    id: UUID = field(default_factory=uuid4)
    thread_id: UUID | None = None
    comment: str | None = None

    @staticmethod
    def get_table_name(schema: str) -> sql.Identifier:
        return ChatTable.FEEDBACKS.under(schema)

    @classmethod
    def from_payload(cls, payload: FeedbackPayload) -> Self:
        return cls(
            id=Codec.uuid_opt(payload.id) or uuid4(),
            for_id=Codec.uuid(payload.forId),
            thread_id=Codec.uuid_opt(payload.threadId),
            value=payload.value,
            comment=payload.comment,
        )

    def to_chainlit(self) -> FeedbackDict:
        return FeedbackDict(
            forId=Codec.uuid_str(self.for_id),
            id=Codec.uuid_str(self.id),
            value=self.value,
            comment=self.comment,
        )

    @classmethod
    def ddl(cls, schema: str) -> tuple[sql.Composed, ...]:
        table = cls.get_table_name(schema)
        return (
            sql.SQL(
                """
                create table if not exists {table} (
                    id        uuid primary key,
                    for_id    uuid not null,
                    value     smallint not null,
                    thread_id uuid,
                    comment   text
                )
                """
            ).format(table=table),
            sql.SQL(
                """
                create index if not exists idx_feedbacks_for_id
                    on {table} (for_id)
                """
            ).format(table=table),
        )
