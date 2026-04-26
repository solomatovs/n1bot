"""Entry-points loader для tool-плагинов.

Tools поставляются как pip-installable Python-пакеты, объявляющие
entry-point в группе ``boba.tools``::

    [project.entry-points."boba.tools"]
    chromadb = "boba.ext.chromadb:register_tools"

Где ``register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]`` —
функция, регистрирующая один или несколько :class:`ToolSource`.

Discovery — через :func:`importlib.metadata.entry_points`. Никакой
файловой папки с плагинами больше нет: оператор устанавливает
расширения обычным ``pip install`` (например, ``pip install -e
./extensions/<name>`` для in-tree dev-установки), и при старте
:class:`ToolPluginLoader` собирает их в один :class:`ToolsService`.

Жизненный цикл: discovery + register-вызовы происходят в конструкторе
:class:`ToolPluginLoader`; собранный :class:`ToolsService` кэшируется
и отдаётся :meth:`tools_service`.

Trust-модель:

1. Расширения — это код, не пользовательский ввод. Поставляются через
   trusted-канал (внутренний PyPI / git с code-review/подписями).
2. :class:`ExtensionContext` намеренно урезан: не содержит credentials
   (``api_key``, токены) и файлового workspace — pip-пакет сам знает
   свою корневую директорию через ``__file__`` для package data.
   Конфиг расширения объявляется как :class:`ConfigSection` и
   подхватывается :class:`ConfigFactory` через entry-point group
   ``boba.config_sections`` (см. :mod:`boba.infra.config`); внутри
   ``register_tools`` он достаётся через
   ``ctx.config.section(MyExtSection)``.
"""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import cast

from boba.domain.core.tools import ToolFactory, ToolSource, ToolsService
from boba.infra.config import ConfigBundle

logger = logging.getLogger(__name__)


ENTRY_POINTS_GROUP = "boba.tools"

RegisterToolsFn = Callable[["ExtensionContext"], Iterable[ToolSource]]


class ToolPluginError(Exception):
    """Базовая ошибка tool-plugin инфры. Несёт ``entry_point_name`` —
    имя entry-point, на котором произошёл сбой.
    """

    def __init__(self, entry_point_name: str, message: str) -> None:
        super().__init__(f"tool plugin {entry_point_name!r}: {message}")
        self.entry_point_name = entry_point_name


class ToolPluginLoadError(ToolPluginError):
    """Ошибка загрузки entry-point: ``ep.load()`` упал или вернул
    не callable.
    """


class ToolPluginRegisterError(ToolPluginError):
    """Ошибка инстанцирования: ``register_tools(ctx)`` бросил исключение
    или вернул что-то некорректное.
    """


@dataclass(frozen=True)
class ExtensionContext:
    """Контракт окружения для ``register_tools(ctx)``.

    ``config`` — application-singleton, строится один раз на старте,
    передаётся в register-вызовы каждого плагина и дальше не меняется.
    Per-request зависимости (project_workspace пользователя) сюда не
    входят — tool-инстансы получают рабочий workspace через
    :class:`~boba.domain.core.tools.ToolContext` в :meth:`Tool.execute`.

    Через ``config`` плагин достаёт:

    - ``ctx.config.section(MyExtSection)`` — типизированный DTO своей
      :class:`~boba.domain.core.config.ConfigSection`;
    - ``ctx.config.app`` — общий :class:`AppConfig` (workspaces, llm, ...);
    - ``ctx.config.agent`` — :class:`AgentConfig` для лимитов.

    LLM-credentials отдельно не передаются — они уже внутри
    ``ctx.config.app.llm``; читать их плагину не запрещено, но и
    раздавать «на всякий случай» в дискретное поле смысла нет.
    """

    config: ConfigBundle


class ToolPluginLoader:
    """Discovery tool-плагинов через Python entry-points группы
    ``boba.tools`` и сборка :class:`ToolsService` один раз на старте
    процесса.

    Discovery + register-вызовы происходят в конструкторе; результат
    кэшируется. :meth:`tools_service` — просто отдаёт закэшированный
    инстанс.
    """

    def __init__(self, ctx: ExtensionContext) -> None:
        self._ctx = ctx
        self._tool_sources: list[ToolSource] = []
        self._discover()
        self._tools_service = self._build_tools_service()

    def tools_service(self) -> ToolsService:
        """Закэшированный :class:`ToolsService` со всеми ``register_tools``
        зарегистрированных entry-point'ов.
        """
        return self._tools_service

    def _build_tools_service(self) -> ToolsService:
        factory = ToolFactory()
        for source in self._tool_sources:
            factory.register(source)
        service = ToolsService(factory)
        service.rebuild_catalog()
        return service

    def _discover(self) -> None:
        for ep in importlib.metadata.entry_points(group=ENTRY_POINTS_GROUP):
            try:
                self._load_and_register(ep)
            except ToolPluginError as e:
                logger.warning("%s; skipped", e)

    def _load_and_register(self, ep: importlib.metadata.EntryPoint) -> None:
        try:
            obj = ep.load()
        except Exception as e:
            raise ToolPluginLoadError(
                ep.name, f"entry-point load failed: {type(e).__name__}: {e}"
            ) from e
        if not callable(obj):
            raise ToolPluginLoadError(
                ep.name,
                f"entry-point target is not callable: {type(obj).__name__}",
            )
        # ep.load() returns ``object``; статически сузить до
        # ``RegisterToolsFn`` нельзя, runtime-проверка callable выше.
        register = cast("RegisterToolsFn", obj)
        try:
            for source in register(self._ctx):
                self._tool_sources.append(source)
        except Exception as e:
            raise ToolPluginRegisterError(
                ep.name,
                f"register_tools(ctx) failed: {type(e).__name__}: {e}",
            ) from e
