"""DTO и value-объекты chainlit-приложения: IDs, доменный User, persisted DTO."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NewType

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from boba.workspace.contract import WorkspaceId

__all__ = [
    "StoredUser",
    "ThreadId",
    "ThreadIndexEntry",
    "ThreadMeta",
    "ThreadMetaCorruptedError",
    "User",
    "UserId",
]


UserId = NewType("UserId", str)
"""Идентификатор пользователя."""


ThreadId = NewType("ThreadId", str)
"""Идентификатор thread_id chainlit."""


@dataclass(frozen=True)
class User:
    """Доменный пользователь приложения."""

    username: str


_WIRE_CONFIG = ConfigDict(
    alias_generator=to_camel,
    populate_by_name=True,
    frozen=True,
)


class StoredUser(BaseModel):
    """Persisted user record (`users.json`). Wire — camelCase."""

    model_config = _WIRE_CONFIG

    id: UserId
    identifier: str
    created_at: str
    display_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ThreadMetaCorruptedError(Exception):
    """Не удалось декодировать ThreadMeta (повреждённый wire-формат)."""


class ThreadMeta(BaseModel):
    """Метаданные thread'а. Сообщения живут в `HistoryService`, не здесь."""

    model_config = _WIRE_CONFIG

    id: ThreadId
    workspace_id: WorkspaceId
    user_id: UserId | None
    user_identifier: str | None
    name: str | None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    system_prompt: str | None = None
    # Whitelist tool_id'ов: None ⇒ все доступные (backward-compat).
    # Список ⇒ ровно эти; элементы вне текущего каталога молча игнорируются.
    enabled_tool_ids: list[str] | None = None
    created_at: str
    updated_at: str


class ThreadIndexEntry(BaseModel):
    """Снимок ThreadMeta для глобального индекса (`threads-index.json`)."""

    model_config = _WIRE_CONFIG

    workspace_id: WorkspaceId
    user_id: UserId | None
    user_identifier: str | None
    name: str | None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    system_prompt: str | None = None
    enabled_tool_ids: list[str] | None = None
    created_at: str
    updated_at: str

    @classmethod
    def from_meta(cls, meta: ThreadMeta) -> ThreadIndexEntry:
        try:
            return cls.model_validate(meta, from_attributes=True)
        except ValidationError as e:
            raise ThreadMetaCorruptedError(
                f"ThreadIndexEntry.from_meta: не удалось построить запись индекса "
                f"из ThreadMeta (id={meta.id!r}): {e}"
            ) from e

    def to_meta(self, thread_id: ThreadId) -> ThreadMeta:
        return ThreadMeta(id=thread_id, **self.model_dump())
