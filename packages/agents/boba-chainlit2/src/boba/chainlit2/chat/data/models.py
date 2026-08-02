"""
Dataclass-модели chainlit-данных:
    users
    threads
    elements
    feedbacks

Модели шагов нет: сообщения треда хранит langgraph-checkpointer.
"""

from dataclasses import Field, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal, Self, TypeVar
from uuid import UUID, uuid4

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
from psycopg import sql
from psycopg.types.json import Jsonb

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
        return UUID(value) if value else None

    @staticmethod
    def uuid_str(value: UUID) -> str:
        return str(value)

    @staticmethod
    def uuid_str_opt(value: UUID | None) -> str | None:
        return str(value) if value is not None else None

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
        return sql.SQL(", ").join(
            sql.Identifier(prefix, f.name) if prefix else sql.Identifier(f.name)
            for f in fields(cls)
        )

    @classmethod
    def all_placeholders(cls) -> sql.Composable:
        return sql.SQL(", ").join(sql.Placeholder(f.name) for f in fields(cls))

    @classmethod
    def insert_columns(cls) -> sql.Composable:
        return sql.SQL(", ").join(
            sql.Identifier(f.name) for f in cls._insertable()
        )

    @classmethod
    def insert_placeholders(cls) -> sql.Composable:
        return sql.SQL(", ").join(sql.Placeholder(f.name) for f in cls._insertable())

    @classmethod
    def _insertable(cls) -> list[Field[Any]]:
        return [f for f in fields(cls) if not f.metadata.get("db_generated")]

    @classmethod
    def all_assignments(cls, *, exclude: tuple[str, ...] = ()) -> sql.Composable:
        return sql.SQL(", ").join(
            sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(f.name))
            for f in fields(cls)
            if f.name not in exclude
        )

    def all_params(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            jsonb = f.metadata.get("jsonb") and value is not None
            out[f.name] = Jsonb(value) if jsonb else value
        return out

    @classmethod
    def ddl(cls, schema: str) -> tuple[sql.Composed, ...]:
        raise NotImplementedError


@dataclass(slots=True)
class User(Row):
    """Пользователь chainlit."""

    identifier: str
    user_uuid: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=Codec.now)
    meta: dict[str, Any] = field(default_factory=dict, metadata={"jsonb": True})
    id: int = field(default=0, metadata={"db_generated": True})

    @staticmethod
    def get_table_name(schema: str) -> sql.Identifier:
        return sql.Identifier(schema, "users")

    @classmethod
    def from_chainlit(cls, user: ChainlitUser) -> Self:
        return cls(
            identifier=user.identifier,
            meta=user.metadata,
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
                    id         integer generated always as identity primary key,
                    user_uuid  uuid not null unique,
                    identifier text not null unique,
                    created_at timestamptz not null,
                    meta       jsonb not null default '{{}}'::jsonb
                )
                """
            ).format(table=cls.get_table_name(schema)),
        )


@dataclass(slots=True)
class Thread(Row):
    """Диалог (тред)."""

    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=Codec.now)
    name: str | None = None
    user_id: int | None = None
    tags: list[str] | None = None
    meta: dict[str, Any] | None = field(default=None, metadata={"jsonb": True})

    @staticmethod
    def get_table_name(schema: str) -> sql.Identifier:
        return sql.Identifier(schema, "threads")

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
            userId=str(self.user_id) if self.user_id is not None else None,
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
                    user_id    integer,
                    tags       text[],
                    meta       jsonb
                )
                """
            ).format(table=table),
            sql.SQL(
                "create index if not exists idx_threads_user_id on {table} (user_id)"
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

    PATH_TEMPLATE: ClassVar[str] = "{user_id}/{thread_id}/{id}"
    """Путь файла вычисляется из идентификаторов и в базе не хранится."""

    @staticmethod
    def get_table_name(schema: str) -> sql.Identifier:
        return sql.Identifier(schema, "elements")

    @classmethod
    def object_key(cls, user_id: object, thread_id: object, element_id: object) -> str:
        return cls.PATH_TEMPLATE.format(
            user_id=user_id, thread_id=thread_id, id=element_id
        )

    @classmethod
    def from_chainlit(cls, data: ElementDict) -> Self:
        return cls(
            id=Codec.uuid(data.get("id")),
            thread_id=Codec.uuid_opt(data.get("threadId")),
            for_id=Codec.uuid_opt(data.get("forId")),
            type=Codec.require(data.get("type")),
            chainlit_key=data.get("chainlitKey"),
            name=Codec.require(data.get("name")),
            display=Codec.require(data.get("display")),
            size=data.get("size"),
            language=data.get("language"),
            page=data.get("page"),
            props=data.get("props"),
            mime=data.get("mime"),
        )

    def to_chainlit(self) -> ElementDict:
        return {
            "id": Codec.uuid_str(self.id),
            "threadId": Codec.uuid_str_opt(self.thread_id),
            "type": self.type,
            "chainlitKey": self.chainlit_key,
            "name": self.name,
            "display": self.display,
            "size": self.size,
            "language": self.language,
            "page": self.page,
            "props": self.props,
            "forId": Codec.uuid_str_opt(self.for_id),
            "mime": self.mime,
        }

    @classmethod
    def ddl(cls, schema: str) -> tuple[sql.Composed, ...]:
        table = cls.get_table_name(schema)
        return (
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {table} (
                    id uuid PRIMARY KEY,
                    name text NOT NULL,
                    type text NOT NULL,
                    display text NOT NULL,
                    thread_id uuid,
                    for_id uuid,
                    chainlit_key text,
                    size text,
                    language text,
                    page integer,
                    props jsonb,
                    mime text
                )
                """
            ).format(table=table),
            sql.SQL(
                "CREATE INDEX IF NOT EXISTS idx_elements_thread_id "
                "ON {table} (thread_id)"
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
        return sql.Identifier(schema, "feedbacks")

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
                CREATE TABLE IF NOT EXISTS {table} (
                    id uuid PRIMARY KEY,
                    for_id uuid NOT NULL,
                    value smallint NOT NULL,
                    thread_id uuid,
                    comment text
                )
                """
            ).format(table=table),
            sql.SQL(
                "CREATE INDEX IF NOT EXISTS idx_feedbacks_for_id ON {table} (for_id)"
            ).format(table=table),
        )
