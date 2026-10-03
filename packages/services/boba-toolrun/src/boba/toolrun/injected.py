"""Подстановка injected-конфига в kwargs вызова и правка схемы инструмента.

Служебные параметры тела подставляют источники ParamSource; цепочка обвязок
ставит их сразу поверх обёртки запуска. InjectedConfig — источник значений
конфига: partial кладёт статические значения в kwargs до того, как обёртка
разложит их на argv и stdin, и снимает injected-поля с args_schema —
langchain валидирует вход по схеме до тела; значение с keytab-секцией
довооружается на каждый вызов (ConfigArming). AsyncInjected — база обвязок,
которым значение нужно дождаться на каждом вызове (билет, соединения
субъекта, контекст вызова).

Ошибки:
ToolConfigError — у injected-параметра нет значения у загрузчика.
InjectedAsyncOnlyError — тело с ожидаемым значением вызвано синхронно.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Callable, Sequence
from typing import Any, Protocol, TypeAlias

from langchain_core.tools import BaseTool

from boba.toolkit.entry import ToolArgv
from boba.toolrun.wrapping import CallHooks, ToolBody, ToolSchema

__all__ = [
    "AsyncInjected",
    "ConfigArming",
    "ConfigResolver",
    "InjectedAsyncOnlyError",
    "InjectedConfig",
    "ParamSource",
    "StaticConfig",
    "ToolConfigError",
]

logger = logging.getLogger(__name__)

ConfigResolver: TypeAlias = Callable[[str, Any], object]
"""(имя параметра, аннотация) -> значение; собирает загрузчик из конфига."""


class ToolConfigError(Exception):
    """Injected-параметру инструмента нечего подставить."""


class InjectedAsyncOnlyError(Exception):
    """Обвязка поставлена, но тело вызвано путём, где значение не дождаться."""


class ParamSource(Protocol):
    """Источник значений служебных параметров инструмента.

    Реализация ставит свои обвязки на тела инструментов и правит схему для
    LLM: снимает или заменяет поля, которые подставит сама. Источники
    применяет цепочка обвязок (ToolChain.launch) по порядку, поверх обёртки
    запуска.
    """

    @abstractmethod
    def bind_all(self, tools: Sequence[BaseTool]) -> None: ...


class ConfigArming(Protocol):
    """Довооружение значения injected-конфига на вызов.

    Значение, которое нельзя отдавать телу как есть (keytab в секции
    соединения), источник конфига заменяет на каждом вызове итогом armed().
    """

    @abstractmethod
    def needs(self, value: object) -> bool:
        """Нужно ли значению довооружение на вызов."""

    @abstractmethod
    async def armed(self, param: str, value: object) -> object:
        """Значение параметра для одного вызова."""


class StaticConfig(ConfigArming):
    """Реализация ConfigArming без довооружения: конфиг уезжает телу как есть."""

    def needs(self, value: object) -> bool:
        return False

    async def armed(self, param: str, value: object) -> object:
        return value


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
        msg = (
            f"tool {name!r}: injected {self._param!r} is built by "
            f"{type(self).__name__} in the async body only, but the tool was "
            "invoked synchronously"
        )
        raise InjectedAsyncOnlyError(msg)

    async def before_async(
        self,
        name: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> None:
        kwargs[self._param] = await self.value(name, kwargs)


class InjectedConfig(ParamSource):
    """Реализация ParamSource значениями конфига: partial поверх обёртки
    запуска плюс снятие полей со схемы.

    Создаёт его загрузчик инструментов из резолвера конфига и довооружения
    (ConfigArming). Значение каждого injected-параметра резолвится один раз
    при постановке; статическое кладётся в kwargs partial'ом, требующее
    довооружения — обвязкой на каждый вызов.
    """

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

    class _Armed(AsyncInjected):
        def __init__(self, arming: ConfigArming, param: str, base: object) -> None:
            super().__init__(param, base)
            self._arming = arming

        async def value(self, name: str, kwargs: dict[str, object]) -> object:
            return await self._arming.armed(self._param, self._base)

    def __init__(self, resolve: ConfigResolver, arming: ConfigArming) -> None:
        self._resolve = resolve
        self._arming = arming

    def bind_all(self, tools: Sequence[BaseTool]) -> None:
        for tool in tools:
            self._bind(tool)

    def _bind(self, tool: BaseTool) -> None:
        schema = ToolSchema.of(tool)
        if schema is None:
            return

        values: dict[str, object] = {}
        for param, annotation in ToolArgv.injected_fields(schema).items():
            values[param] = self._resolve(param, annotation)

        if not values:
            return

        static: dict[str, object] = {}
        for param, value in values.items():
            if not self._arming.needs(value):
                static[param] = value
                continue

            ToolBody.hook_all([tool], self._Armed(self._arming, param, value))
            logger.info("tool %s: config %s is armed per call", tool.name, param)

        if static:
            ToolBody.hook_all([tool], self._Partial(static))

        tool.args_schema = ToolSchema.rebuild(schema, {}, values)
