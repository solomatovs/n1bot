"""Значения контекста вызова injected-параметрами тела.

Тело, которому нужно знать, от чьего имени, в какой области и над каким
workspace идёт вызов, объявляет параметр моделью контекста:
`subject: Annotated[Subject, Injected]` (пользователь, роли),
`scope: Annotated[Scope, Injected]` (область вызова: тред, задача),
`root: Annotated[WorkspaceRoot, Injected]` (корень workspace запуска).
Манифест плагина называет такие параметры ролью (InjectedRole), хост
подставляет значение на каждый вызов, как соединения субъекта; оно уезжает
телу в аргументах вызова. Каждый инструмент хоста работает в workspace
области вызова, поэтому в его схему ставится параметр области
(CallFields.scoped): по нему сервер выбирает область.

Ошибки:
RefusalError — вызов идёт вне контекста CallContext.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import ClassVar

from boba.canvas.keys import WorkspaceRoot
from boba.identity.context import CallContexts
from boba.toolkit.manifest import InjectedRole
from boba.toolrun.call_id import CallFields
from boba.toolrun.hosted import HostedTool
from boba.toolrun.injected import AsyncInjected, ParamSource
from boba.toolrun.wrapping import ToolBody

__all__ = ["CallContextValues"]

logger = logging.getLogger(__name__)


class CallContextValues(ParamSource):
    """Реализация ParamSource значениями контекста вызова: ставит обвязку на
    параметры ролей контекста и подставляет значение на каждый вызов; в
    схему ставит параметр области, из которого сервер берёт область вызова.

    Создаёт его загрузчик инструментов из держателя контекста процесса и
    ставит первым из источников.
    """

    ROLES: ClassVar[frozenset[InjectedRole]] = frozenset(
        {InjectedRole.SUBJECT, InjectedRole.SCOPE, InjectedRole.WORKSPACE_ROOT}
    )
    """Роли контекста, которые тело может объявить injected-параметром."""

    class _Value(AsyncInjected):
        def __init__(
            self, param: str, role: InjectedRole, contexts: CallContexts
        ) -> None:
            super().__init__(param, role)
            self._role = role
            self._contexts = contexts

        async def value(self, name: str, kwargs: dict[str, object]) -> object:
            context = self._contexts.current()
            if self._role is InjectedRole.SUBJECT:
                return context.subject

            if self._role is InjectedRole.SCOPE:
                return context.scope

            return WorkspaceRoot.current()

    def __init__(self, contexts: CallContexts) -> None:
        self._contexts = contexts
        self._bodies = ToolBody()
        self._fields = CallFields()

    def bind_all(self, tools: Sequence[HostedTool]) -> None:
        for tool in tools:
            self._bind_one(tool)

    def _bind_one(self, tool: HostedTool) -> None:
        for param, role in tool.inputs.injected.items():
            if role not in self.ROLES:
                continue

            self._bodies.hook_all([tool], self._Value(param, role, self._contexts))
            logger.info("tool %s: %s is the %s of the call", tool.name, param, role)

        tool.args_schema = self._fields.scoped(tool.args_schema)
