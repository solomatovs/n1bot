"""Подстановка секции плагина в kwargs вызова: источник значений роли config.

Служебные параметры тела подставляют источники ParamSource; цепочка обвязок
ставит их сразу поверх обёртки запуска. InjectedConfig — источник значений
конфига: параметрам роли config инструмента плагина partial кладёт секцию
[tool.<id>] значениями JSON; секцию с keytab-профилями соединений
довооружение (SectionArming) заменяет на каждый вызов секцией с билетами.
AsyncInjected — база обвязок, которым значение нужно дождаться на каждом
вызове (билет, соединения субъекта, контекст вызова).

Ошибки:
ToolConfigError — служебному параметру нечего подставить.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from boba.toolkit.manifest import InjectedRole
from boba.toolrun.hosted import HostedCall, HostedTool, PluginSection
from boba.toolrun.wrapping import CallHooks, ToolBody

__all__ = [
    "AsyncInjected",
    "ConfigArming",
    "InjectedConfig",
    "ParamSource",
    "SectionArming",
    "StaticConfig",
    "StaticSection",
    "ToolConfigError",
]

logger = logging.getLogger(__name__)


class ToolConfigError(Exception):
    """Injected-параметру инструмента нечего подставить."""


class ParamSource(Protocol):
    """Источник значений служебных параметров инструмента.

    Реализация ставит свои обвязки на тела инструментов и правит схему для
    LLM: снимает или заменяет поля, которые подставит сама. Источники
    применяет цепочка обвязок (ToolChain.launch) по порядку, поверх обёртки
    запуска.
    """

    @abstractmethod
    def bind_all(self, tools: Sequence[HostedTool]) -> None: ...


class ConfigArming(Protocol):
    """Довооружение значения на вызов.

    Значение, которое нельзя отдавать телу как есть (keytab в профиле
    соединения), заменяется на каждом вызове итогом armed(). Реализует его
    ServiceTickets; пользуются довооружение секции плагина (SectionArming)
    и заданные соединения сервера.
    """

    @abstractmethod
    def needs(self, value: object) -> bool:
        """Нужно ли значению довооружение на вызов."""

    @abstractmethod
    async def armed(self, param: str, value: object) -> object:
        """Значение параметра для одного вызова."""


class StaticConfig(ConfigArming):
    """Реализация ConfigArming без довооружения: значение уезжает как есть."""

    def needs(self, value: object) -> bool:
        return False

    async def armed(self, param: str, value: object) -> object:
        return value


class SectionArming(Protocol):
    """Довооружение секции плагина на вызов: секция с keytab-профилями
    соединений уезжает телу секцией с билетами вызова."""

    @abstractmethod
    def needs(self, section: PluginSection) -> bool:
        """Нужно ли секции довооружение на вызов."""

    @abstractmethod
    async def armed(self, param: str, section: PluginSection) -> Mapping[str, Any]:
        """Значения секции для одного вызова."""


class StaticSection(SectionArming):
    """Реализация SectionArming без довооружения: секция уезжает как есть."""

    def needs(self, section: PluginSection) -> bool:
        return False

    async def armed(self, param: str, section: PluginSection) -> Mapping[str, Any]:
        return section.values


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

    async def before(self, call: HostedCall) -> None:
        call.kwargs[self._param] = await self.value(call.tool, call.kwargs)


class InjectedConfig(ParamSource):
    """Реализация ParamSource секцией плагина: partial поверх обёртки запуска.

    Создаёт его загрузчик инструментов с довооружением (SectionArming).
    Параметр роли config получает секцию плагина из входов инструмента:
    статическую — partial'ом, требующую довооружения — обвязкой на каждый
    вызов. В схеме хоста этих параметров нет: модель их не видит.
    """

    class _Partial(CallHooks[None]):
        def __init__(self, values: dict[str, object]) -> None:
            self._values = values

        async def before(self, call: HostedCall) -> None:
            for key, value in self._values.items():
                call.kwargs.setdefault(key, value)

    class _Armed(AsyncInjected):
        def __init__(
            self, arming: SectionArming, param: str, section: PluginSection
        ) -> None:
            super().__init__(param, section)
            self._arming = arming
            self._section = section

        async def value(self, name: str, kwargs: dict[str, object]) -> object:
            return await self._arming.armed(self._param, self._section)

    def __init__(self, arming: SectionArming) -> None:
        self._arming = arming
        self._bodies = ToolBody()

    def bind_all(self, tools: Sequence[HostedTool]) -> None:
        for tool in tools:
            self._bind(tool)

    def _bind(self, tool: HostedTool) -> None:
        params = tool.inputs.role_params(InjectedRole.CONFIG)
        if not params:
            return

        section = tool.inputs.section
        if not self._arming.needs(section):
            static: dict[str, object] = {}
            for param in params:
                static[param] = section.values

            self._bodies.hook_all([tool], self._Partial(static))
            return

        for param in params:
            self._bodies.hook_all([tool], self._Armed(self._arming, param, section))
            logger.info("tool %s: config %s is armed per call", tool.name, param)
