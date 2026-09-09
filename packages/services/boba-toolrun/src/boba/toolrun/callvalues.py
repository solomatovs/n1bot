"""Значения контекста вызова injected-параметрами тела.

Тело, которому нужно знать, от чьего имени, в какой области и над каким
workspace идёт вызов, объявляет параметр моделью контекста:
`subject: Annotated[Subject, Injected]` (пользователь, роли),
`scope: Annotated[Scope, Injected]` (тред, задача),
`root: Annotated[WorkspaceRoot, Injected]` (корень workspace запуска).
Хост подставляет значение на каждый вызов, как соединения субъекта; оно
уезжает телу каналом конфига вместе с остальными injected-параметрами.

Ошибки:
RefusalError — вызов идёт вне контекста CallContext.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from langchain_core.tools import BaseTool

from boba.canvas.keys import WorkspaceRoot
from boba.identity.context import CallContext, Scope, Subject
from boba.toolkit.entry import ToolArgv
from boba.toolrun.injected import AsyncInjected
from boba.toolrun.wrapping import ToolBody, ToolSchema

__all__ = ["CallContextValues"]

logger = logging.getLogger(__name__)

ValueOf = Callable[[CallContext], object]


class CallContextValues(AsyncInjected):
    """Обвязка параметра контекста: снимает его со схемы и подставляет на вызов."""

    SOURCES: ClassVar[dict[type, ValueOf]] = {
        Subject: lambda context: context.subject,
        Scope: lambda context: context.scope,
        WorkspaceRoot: lambda context: WorkspaceRoot.current(),
    }
    """Модели контекста, которые тело может объявить injected-параметром."""

    def __init__(self, param: str, model: type) -> None:
        super().__init__(param, model)
        self._model = model

    @classmethod
    def bind_all(cls, tools: Sequence[BaseTool]) -> None:
        """Зовётся до InjectedConfig: контексту нечего взять из toml, и поле
        обязано уйти со схемы раньше, чем резолвер конфига его увидит."""
        for tool in tools:
            cls._bind_one(tool)

    @classmethod
    def _bind_one(cls, tool: BaseTool) -> None:
        schema = ToolSchema.of(tool)
        if schema is None:
            return

        params = cls._context_params(ToolArgv.injected_fields(schema))
        if not params:
            return

        for param, model in params.items():
            ToolBody.hook_all([tool], cls(param, model))
            logger.info(
                "tool %s: %s is the %s of the call", tool.name, param, model.__name__
            )

        tool.args_schema = ToolSchema.rebuild(schema, {}, params)

    @classmethod
    def _context_params(cls, fields: dict[str, Any]) -> dict[str, type]:
        params: dict[str, type] = {}
        for name, annotation in fields.items():
            if not isinstance(annotation, type):
                continue

            if annotation in cls.SOURCES:
                params[name] = annotation

        return params

    async def value(self, name: str, kwargs: dict[str, object]) -> object:
        return self.SOURCES[self._model](CallContext.current())
