"""Данные текущей сессии chainlit: пользователь, тред."""

from __future__ import annotations

from typing import Final

import chainlit as cl
from chainlit.context import ChainlitContextException

__all__ = [
    "UserMetadataField",
    "current_thread_id",
    "current_user_id",
    "current_user_label",
    "current_user_roles",
]


class UserMetadataField:
    "Ключи metadata у cl.User; контракт chainlit."

    PROVIDER: Final = "provider"
    ROLES: Final = "roles"


def _current_user() -> cl.User | cl.PersistedUser | None:
    try:
        return cl.user_session.get("user")
    except ChainlitContextException:
        return None


def current_user_roles() -> frozenset[str]:
    user = _current_user()
    if user is None:
        return frozenset()

    metadata = user.metadata
    if metadata is None:
        metadata = {}

    roles = metadata.get(UserMetadataField.ROLES)
    if not roles:
        roles = []
    if isinstance(roles, str):
        return frozenset({roles})

    return frozenset(str(r) for r in roles)


def current_user_id() -> str | None:
    user = _current_user()
    return getattr(user, "id", None)


def current_user_label() -> str:
    """Логин для логов; пустая строка — запрос идёт вне сессии chainlit."""
    user = _current_user()
    if user is None:
        return ""
    identifier = getattr(user, "identifier", "")
    if identifier:
        return str(identifier)
    return str(getattr(user, "id", ""))


def current_thread_id() -> str | None:
    try:
        return cl.context.session.thread_id
    except ChainlitContextException:
        return None
