from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from boba.patterns import StrId


class UserId(StrId):
    "Идентификатор пользователя"


@dataclass(frozen=True)
class StoredUser:
    """Persisted user record."""

    id: UserId
    identifier: str
    created_at: str
    display_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ThreadId(StrId):
    "Идентификатор thread_id chainlit"

