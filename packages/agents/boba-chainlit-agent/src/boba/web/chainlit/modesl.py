from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NewType

UserId = NewType("UserId", str)
"""Идентификатор пользователя."""


ThreadId = NewType("ThreadId", str)
"""Идентификатор thread_id chainlit."""


@dataclass(frozen=True)
class StoredUser:
    """Persisted user record."""

    id: UserId
    identifier: str
    created_at: str
    display_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
