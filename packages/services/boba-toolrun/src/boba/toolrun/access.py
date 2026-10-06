"""Защита инструмента на вызове: решение о доступе — у boba.access.ToolAccess."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from boba.access import AccessSubject, ToolAccess
from boba.toolrun.hosted import HostedCall, HostedTool
from boba.toolrun.wrapping import CallHooks, ToolBody

logger = logging.getLogger(__name__)

__all__ = ["ToolAccessDeniedError", "ToolAccessGuard"]


class ToolAccessDeniedError(Exception):
    """Инструмент вызван субъектом, которому он не разрешён."""


class ToolAccessGuard:
    """Обвязка прав: проверяет доступ к инструменту в момент вызова.

    Создаётся цепочкой обвязок (ToolChain) с источником субъекта вызова;
    права (ToolAccess) приходят на постановку — их считает загрузчик по
    собранным инструментам.
    """

    def __init__(self, subject_source: Callable[[], AccessSubject]) -> None:
        self._subject_source = subject_source
        self._bodies = ToolBody()

    class _Hooks(CallHooks[None]):
        def __init__(
            self,
            access: ToolAccess,
            subject_source: Callable[[], AccessSubject],
        ) -> None:
            self._access = access
            self._subject_source = subject_source

        async def before(self, call: HostedCall) -> None:
            name = call.tool
            subject = self._subject_source()
            roles = frozenset(subject.roles)
            profile = subject.profile
            if self._access.decide(name, roles, profile).headless:
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

    def guard_all(
        self, tools: Sequence[HostedTool], access: ToolAccess
    ) -> list[HostedTool]:
        """Права проверяются на вызове: субъект берётся источником, не полями."""
        hooks = self._Hooks(access, self._subject_source)
        return self._bodies.hook_all(tools, hooks)
