"""Защита инструмента на вызове: решение о доступе — у boba.access.ToolAccess."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from langchain_core.tools import BaseTool

from boba.access import AccessSubject, ToolAccess
from boba.toolrun.wrapping import CallHooks, ToolBody

logger = logging.getLogger(__name__)

__all__ = ["ToolAccessDeniedError", "ToolAccessGuard"]


class ToolAccessDeniedError(Exception):
    """Инструмент вызван субъектом, которому он не разрешён."""


class ToolAccessGuard:
    """Проверка прав в момент вызова инструмента."""

    class _Hooks(CallHooks[None]):
        def __init__(
            self,
            access: ToolAccess,
            subject_source: Callable[[], AccessSubject],
        ) -> None:
            self._access = access
            self._subject_source = subject_source

        def before(
            self,
            name: str,
            args: tuple[object, ...],
            kwargs: dict[str, object],
        ) -> None:
            subject = self._subject_source()
            roles = frozenset(subject.roles)
            profile = subject.profile
            if self._access.allowed(name, roles, profile):
                return

            shown_roles = ", ".join(sorted(roles))
            if not shown_roles:
                shown_roles = "none"

            shown_profile = profile
            if not shown_profile:
                shown_profile = "none"

            logger.warning(
                "access denied to tool %r: roles [%s] and profile %s grant no access",
                name,
                shown_roles,
                shown_profile,
            )
            msg = (
                f"tool {name!r} is not available for your roles [{shown_roles}] "
                f"and profile {shown_profile}"
            )
            raise ToolAccessDeniedError(msg)

    @classmethod
    def guard_all(
        cls,
        tools: Sequence[BaseTool],
        access: ToolAccess,
        subject_source: Callable[[], AccessSubject],
    ) -> list[BaseTool]:
        """Права проверяются на вызове: субъект берётся источником, не полями."""
        hooks = cls._Hooks(access, subject_source)
        return ToolBody.hook_all(tools, hooks)
