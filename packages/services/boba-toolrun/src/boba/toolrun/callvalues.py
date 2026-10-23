"""Значения контекста вызова injected-параметрами тела.

Тело, которому нужно знать, от чьего имени, в какой области и над каким
workspace идёт вызов, объявляет параметр моделью контекста:
`subject: Annotated[Subject, Injected]` (пользователь, роли),
`scope: Annotated[Scope, Injected]` (область вызова: тред, задача),
`root: Annotated[WorkspaceRoot, Injected]` (корень workspace запуска).
Хост подставляет значение на каждый вызов, как соединения субъекта; оно
уезжает телу каналом конфига вместе с остальными injected-параметрами.
Каждый инструмент хоста работает в workspace области вызова, поэтому в его
схему ставится параметр области (CallFields.scoped): по нему сервер выбирает
область.

Ошибки:
RefusalError — вызов идёт вне контекста CallContext.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, ClassVar

from boba.canvas.keys import WorkspaceRoot
from boba.identity.context import CallContexts, Scope, Subject
from boba.toolkit.entry import ToolArgv
from boba.toolrun.call_id import CallFields
from boba.toolrun.hosted import HostedTool, ToolSchema
from boba.toolrun.injected import AsyncInjected, ParamSource
from boba.toolrun.wrapping import ToolBody

__all__ = ["CallContextValues"]

logger = logging.getLogger(__name__)


class CallContextValues(ParamSource):
    """Реализация ParamSource значениями контекста вызова: снимает параметр
    со схемы и подставляет его на каждый вызов; в схему ставит параметр
    области, из которого сервер берёт область вызова.

    Создаёт его загрузчик инструментов из держателя контекста процесса и
    ставит первым из источников: контексту нечего взять из toml, и поле
    обязано уйти со схемы раньше, чем резолвер конфига его увидит.
    """

    MODELS: ClassVar[frozenset[type]] = frozenset({Subject, Scope, WorkspaceRoot})
    """Модели контекста, которые тело может объявить injected-параметром."""

    class _Value(AsyncInjected):
        def __init__(self, param: str, model: type, contexts: CallContexts) -> None:
            super().__init__(param, model)
            self._model = model
            self._contexts = contexts

        async def value(self, name: str, kwargs: dict[str, object]) -> object:
            context = self._contexts.current()
            if self._model is Subject:
                return context.subject

            if self._model is Scope:
                return context.scope

            return WorkspaceRoot.current()

    def __init__(self, contexts: CallContexts) -> None:
        self._contexts = contexts
        self._bodies = ToolBody()
        self._schemas = ToolSchema()
        self._fields = CallFields()

    def bind_all(self, tools: Sequence[HostedTool]) -> None:
        for tool in tools:
            self._bind_one(tool)

    def _bind_one(self, tool: HostedTool) -> None:
        schema = tool.args_schema
        params = self._context_params(ToolArgv.injected_fields(schema))
        if not params:
            tool.args_schema = self._fields.scoped(schema)
            return

        for param, model in params.items():
            self._bodies.hook_all([tool], self._Value(param, model, self._contexts))
            logger.info(
                "tool %s: %s is the %s of the call", tool.name, param, model.__name__
            )

        bound = self._schemas.rebuild(schema, {}, params)
        tool.args_schema = self._fields.scoped(bound)

    def _context_params(self, fields: dict[str, Any]) -> dict[str, type]:
        params: dict[str, type] = {}
        for name, annotation in fields.items():
            if not isinstance(annotation, type):
                continue

            if annotation in self.MODELS:
                params[name] = annotation

        return params
