"""
Dataclass-модели chainlit-данных:
    users
    threads
    steps
    elements
    feedbacks
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
from literalai.observability.step import StepType
from psycopg import sql
from psycopg.types.json import Jsonb

__all__ = [
    "Codec",
    "Element",
    "Feedback",
    "Row",
    "Step",
    "Thread",
    "User",
]


_T = TypeVar("_T")


class Codec:
    """Кодеки значений поле модели ↔ chainlit-dict (UUID/datetime)."""

    @staticmethod
    def require(value: _T | None) -> _T:
        """Обязательный chainlit-ключ: fail-fast, если он None/отсутствует."""
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
    def dt_opt(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    @staticmethod
    def iso(value: datetime) -> str:
        return value.isoformat()

    @staticmethod
    def iso_opt(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)


class Row:
    """База dataclass-моделей: список колонок/плейсхолдеров из полей."""

    __slots__ = ()
    __dataclass_fields__: ClassVar[dict[str, Field[Any]]]

    @classmethod
    def all_columns(cls, prefix: str | None = None) -> sql.Composable:
        """Список колонок (квотинг/склейка psycopg); prefix — алиас таблицы."""
        return sql.SQL(", ").join(
            sql.Identifier(prefix, f.name) if prefix else sql.Identifier(f.name)
            for f in fields(cls)
        )

    @classmethod
    def all_placeholders(cls) -> sql.Composable:
        """Именованные плейсхолдеры под dict-параметры (через psycopg)."""
        return sql.SQL(", ").join(sql.Placeholder(f.name) for f in fields(cls))

    @classmethod
    def all_assignments(cls, *, exclude: tuple[str, ...] = ()) -> sql.Composable:
        """'col = EXCLUDED.col' для ON CONFLICT DO UPDATE, минус exclude."""
        return sql.SQL(", ").join(
            sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(f.name))
            for f in fields(cls)
            if f.name not in exclude
        )

    def all_params(self) -> dict[str, Any]:
        """Значения под placeholders(): jsonb-поля оборачиваются в Jsonb."""
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            jsonb = f.metadata.get("jsonb") and value is not None
            out[f.name] = Jsonb(value) if jsonb else value
        return out

    @classmethod
    def ddl(cls, schema: str) -> tuple[sql.Composed, ...]:
        """DDL модели в заданной схеме: CREATE TABLE + индексы (idempotent)."""
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
        return sql.Identifier(schema, "users")

    @classmethod
    def from_chainlit(cls, user: ChainlitUser) -> Self:
        return cls(
            identifier=user.identifier,
            meta=user.metadata,
        )

    def to_persisted(self) -> PersistedUser:
        return PersistedUser(
            id=Codec.uuid_str(self.id),
            identifier=self.identifier,
            createdAt=Codec.iso(self.created_at),
            metadata=dict(self.meta),
        )

    @classmethod
    def ddl(cls, schema: str) -> tuple[sql.Composed, ...]:
        return (
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {table} (
                    id uuid PRIMARY KEY,
                    identifier text NOT NULL UNIQUE,
                    created_at timestamptz NOT NULL,
                    meta jsonb NOT NULL DEFAULT '{{}}'::jsonb
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
    user_id: UUID | None = None
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
                CREATE TABLE IF NOT EXISTS {table} (
                    id uuid PRIMARY KEY,
                    created_at timestamptz NOT NULL,
                    name text,
                    user_id uuid,
                    tags text[],
                    meta jsonb
                )
                """
            ).format(table=table),
            sql.SQL(
                "CREATE INDEX IF NOT EXISTS idx_threads_user_id ON {table} (user_id)"
            ).format(table=table),
        )


@dataclass(slots=True)
class Step(Row):
    """Шаг диалога (сообщение/инструмент/рассуждение)."""

    type: StepType
    thread_id: UUID
    id: UUID = field(default_factory=uuid4)
    name: str = ""
    parent_id: UUID | None = None
    command: str | None = None
    streaming: bool = False
    wait_for_answer: bool | None = None
    is_error: bool | None = None
    meta: dict[str, Any] = field(default_factory=dict, metadata={"jsonb": True})
    tags: list[str] | None = None
    input: str = ""
    output: str = ""
    created_at: datetime = field(default_factory=Codec.now)
    start: datetime | None = None
    end: datetime | None = None
    generation: dict[str, Any] | None = field(default=None, metadata={"jsonb": True})
    show_input: bool | str | None = field(default=None, metadata={"jsonb": True})
    default_open: bool | None = None
    language: str | None = None

    @staticmethod
    def get_table_name(schema: str) -> sql.Identifier:
        return sql.Identifier(schema, "steps")

    @classmethod
    def from_chainlit(cls, data: StepDict) -> Self:
        return cls(
            id=Codec.uuid(data.get("id")),
            name=data.get("name", ""),
            type=Codec.require(data.get("type")),
            thread_id=Codec.uuid(data.get("threadId")),
            parent_id=Codec.uuid_opt(data.get("parentId")),
            command=data.get("command"),
            streaming=data.get("streaming", False),
            wait_for_answer=data.get("waitForAnswer"),
            is_error=data.get("isError"),
            meta=dict(data.get("metadata") or {}),
            tags=data.get("tags"),
            input=data.get("input", ""),
            output=data.get("output", ""),
            created_at=Codec.dt_opt(data.get("createdAt")) or Codec.now(),
            start=Codec.dt_opt(data.get("start")),
            end=Codec.dt_opt(data.get("end")),
            generation=data.get("generation"),
            show_input=data.get("showInput"),
            default_open=data.get("defaultOpen"),
            language=data.get("language"),
        )

    def to_chainlit(self) -> StepDict:
        return {
            "id": Codec.uuid_str(self.id),
            "name": self.name,
            "type": self.type,
            "threadId": Codec.uuid_str(self.thread_id),
            "parentId": Codec.uuid_str_opt(self.parent_id),
            "command": self.command,
            "streaming": self.streaming,
            "waitForAnswer": self.wait_for_answer,
            "isError": self.is_error,
            "metadata": self.meta,
            "tags": self.tags,
            "input": self.input,
            "output": self.output,
            "createdAt": Codec.iso(self.created_at),
            "start": Codec.iso_opt(self.start),
            "end": Codec.iso_opt(self.end),
            "generation": self.generation,
            "showInput": self.show_input,
            "defaultOpen": self.default_open,
            "language": self.language,
        }

    @classmethod
    def ddl(cls, schema: str) -> tuple[sql.Composed, ...]:
        table = cls.get_table_name(schema)
        return (
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {table} (
                    id uuid PRIMARY KEY,
                    type text NOT NULL,
                    thread_id uuid NOT NULL,
                    name text NOT NULL DEFAULT '',
                    parent_id uuid,
                    command text,
                    streaming boolean NOT NULL DEFAULT false,
                    wait_for_answer boolean,
                    is_error boolean,
                    meta jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                    tags text[],
                    input text NOT NULL DEFAULT '',
                    output text NOT NULL DEFAULT '',
                    created_at timestamptz NOT NULL,
                    start timestamptz,
                    "end" timestamptz,
                    generation jsonb,
                    show_input jsonb,
                    default_open boolean,
                    language text
                )
                """
            ).format(table=table),
            sql.SQL(
                "CREATE INDEX IF NOT EXISTS idx_steps_thread_id ON {table} (thread_id)"
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
    url: str | None = None
    object_key: str | None = None
    size: ElementSize | None = None
    language: str | None = None
    page: int | None = None
    props: dict[str, Any] | None = field(default=None, metadata={"jsonb": True})
    mime: str | None = None

    @staticmethod
    def get_table_name(schema: str) -> sql.Identifier:
        return sql.Identifier(schema, "elements")

    @classmethod
    def from_chainlit(cls, data: ElementDict) -> Self:
        return cls(
            id=Codec.uuid(data.get("id")),
            thread_id=Codec.uuid_opt(data.get("threadId")),
            for_id=Codec.uuid_opt(data.get("forId")),
            type=Codec.require(data.get("type")),
            chainlit_key=data.get("chainlitKey"),
            url=data.get("url"),
            object_key=data.get("objectKey"),
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
            "url": self.url,
            "objectKey": self.object_key,
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
                    url text,
                    object_key text,
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
