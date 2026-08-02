"""Роли текущего пользователя из сессии chainlit."""

from __future__ import annotations

import chainlit as cl
from chainlit.context import ChainlitContextException

__all__ = ["current_user_roles"]

ROLES_KEY = "roles"


def current_user_roles() -> frozenset[str]:
    try:
        user = cl.user_session.get("user")
    except ChainlitContextException:
        return frozenset()
    if user is None:
        return frozenset()
    roles = (user.metadata or {}).get(ROLES_KEY) or []
    if isinstance(roles, str):
        return frozenset({roles})
    return frozenset(str(r) for r in roles)
