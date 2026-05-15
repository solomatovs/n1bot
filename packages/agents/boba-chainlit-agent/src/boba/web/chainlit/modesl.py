from __future__ import annotations

from typing import Any, NewType

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

UserId = NewType("UserId", str)
"""Идентификатор пользователя."""


ThreadId = NewType("ThreadId", str)
"""Идентификатор thread_id chainlit."""


class StoredUser(BaseModel):
    """Persisted user record (`users.json`). Wire — camelCase."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        frozen=True,
    )

    id: UserId
    identifier: str
    created_at: str
    display_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
