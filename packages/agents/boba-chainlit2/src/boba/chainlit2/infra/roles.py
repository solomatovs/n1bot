"""Роли текущего пользователя из сессии chainlit."""

from __future__ import annotations

import logging

import chainlit as cl

logger = logging.getLogger(__name__)

__all__ = ["current_user_roles"]

ROLES_KEY = "roles"


def current_user_roles() -> frozenset[str]:
    try:
        user = cl.user_session.get("user")
    except Exception:
        logger.debug("роли запрошены вне сессии chainlit")
        return frozenset()
    if user is None:
        return frozenset()
    roles = (user.metadata or {}).get(ROLES_KEY) or []
    if isinstance(roles, str):
        return frozenset({roles})
    return frozenset(str(r) for r in roles)
