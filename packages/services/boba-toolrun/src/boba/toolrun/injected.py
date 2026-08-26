"""Подстановка injected-конфига в kwargs вызова и правка схемы инструмента.

InjectedConfig ставится сразу поверх обёртки запуска: partial кладёт
статические значения в kwargs до того, как обёртка разложит их на argv и
stdin, и снимает injected-поля с args_schema — langchain валидирует вход по
схеме до тела. AsyncInjected — база обвязок, которым значение нужно
дождаться на каждом вызове (билет, соединения субъекта): они ставятся до
InjectedConfig, пока injected-поля ещё на схеме.

Ошибки:
ToolConfigError — у injected-параметра нет значения у загрузчика.
InjectedAsyncOnlyError — тело с ожидаемым значением вызвано синхронно.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Callable, Sequence
from typing import Any, TypeAlias

from langchain_core.tools import BaseTool

from boba.toolkit.entry import ToolArgv
from boba.toolrun.wrapping import CallHooks, ToolBody, ToolSchema

__all__ = [
    "AsyncInjected",
    "ConfigResolver",
    "InjectedAsyncOnlyError",
    "InjectedConfig",
    "InjectedValues",
    "ToolConfigError",
]

logger = logging.getLogger(__name__)

ConfigResolver: TypeAlias = Callable[[str, Any], object]
"""(имя параметра, аннотация) -> значение; собирает загрузчик из конфига."""


class ToolConfigError(Exception):
    """Injected-параметру инструмента нечего подставить."""


class InjectedAsyncOnlyError(Exception):
    """Обвязка поставлена, но тело вызвано путём, где значение не дождаться."""


class InjectedValues:
    """Значения injected-параметров одного инструмента по его схеме."""

    @staticmethod
    def of(tool: BaseTool, resolve: ConfigResolver) -> dict[str, object]:
        schema = ToolSchema.of(tool)
        if schema is None:
            return {}

        injected = ToolArgv.injected_fields(schema)

        values: dict[str, object] = {}
        for name, annotation in injected.items():
            values[name] = resolve(name, annotation)

        return values


class AsyncInjected(CallHooks[None]):
    """Обвязка с ожидаемым значением одного injected-параметра на вызов."""

    def __init__(self, param: str, base: object) -> None:
        self._param = param
        self._base = base

    @property
    def param(self) -> str:
        return self._param

    @abstractmethod
    async def value(self, name: str, kwargs: dict[str, object]) -> object:
        """Значение параметра для этого вызова."""

    def before(
        self,
        name: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> None:
        msg = f"tool {name!r}: {self._param} is built in the async body only"
        raise InjectedAsyncOnlyError(msg)

    async def before_async(
        self,
        name: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> None:
        kwargs[self._param] = await self.value(name, kwargs)

    @classmethod
    def bind_each(
        cls,
        tools: Sequence[BaseTool],
        resolve: ConfigResolver,
        accepts: Callable[[object], bool],
        make: Callable[[str, object], AsyncInjected],
    ) -> None:
        """Ставит обвязку make(param, base) на каждый подходящий injected-параметр."""
        for tool in tools:
            for param, base in InjectedValues.of(tool, resolve).items():
                if not accepts(base):
                    continue

                ToolBody.hook_all([tool], make(param, base))
                logger.info(
                    "tool %s: %s of %s is built per call",
                    tool.name,
                    param,
                    cls.__name__,
                )


class InjectedConfig:
    """Партиал конфига поверх обёртки запуска плюс снятие полей со схемы."""

    class _Partial(CallHooks[None]):
        def __init__(self, values: dict[str, object]) -> None:
            self._values = values

        def before(
            self,
            name: str,
            args: tuple[object, ...],
            kwargs: dict[str, object],
        ) -> None:
            for key, value in self._values.items():
                kwargs.setdefault(key, value)

    @classmethod
    def bind_all(cls, tools: Sequence[BaseTool], resolve: ConfigResolver) -> None:
        for tool in tools:
            cls._bind(tool, resolve)

    @classmethod
    def _bind(cls, tool: BaseTool, resolve: ConfigResolver) -> None:
        values = InjectedValues.of(tool, resolve)
        if not values:
            return

        schema = ToolSchema.of(tool)
        if schema is None:
            return

        ToolBody.hook_all([tool], cls._Partial(values))
        tool.args_schema = ToolSchema.rebuild(schema, {}, values)
