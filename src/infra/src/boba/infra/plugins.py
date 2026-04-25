"""Plugin-инфраструктура: discovery .py-плагинов и сборка ToolsService.

Контракт плагина — один .py-файл в plugin workspace, экспортирующий
функцию ``register(ctx: PluginContext) -> Iterable[ToolSource]``.
Один файл может вернуть несколько :class:`ToolSource`, разные
``ToolSourceId``-ы. Версионирование/манифесты не предусмотрены.

Жизненный цикл:

* :class:`PluginLoader` создаётся **один раз** при старте процесса и
  принимает application-singleton :class:`PluginWorkspaceShell`. В
  конструкторе через ``workspace.tree`` находит все ``*.py`` (кроме
  ``_*.py``), читает их через ``workspace.read_text``, ``exec``-ит в
  свежий :class:`ModuleType` и кэширует ссылки на ``register``.
* :meth:`PluginLoader.build_tools_service` зовётся **на каждый запрос**
  с per-request :class:`PluginContext`, прогоняет все ``register(ctx)``,
  собирает :class:`ToolFactory` и возвращает свежий
  :class:`ToolsService`.

Сбой одного плагина (синтакс/exec/register) логируется и пропускается;
остальные плагины грузятся.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import ModuleType
from typing import cast

from boba.domain.agent.models import AgentConfig
from boba.domain.config import AppConfig
from boba.domain.core.tools import ToolFactory, ToolSource, ToolsService
from boba.domain.core.workspace import (
    PluginWorkspaceShell,
    ProjectWorkspaceShell,
    WorkspaceError,
)
from boba.domain.llm.models import SamplingParams

logger = logging.getLogger(__name__)


PluginRegisterFn = Callable[["PluginContext"], Iterable[ToolSource]]


class PluginError(Exception):
    """Базовая ошибка plugin-инфры. Несёт ``rel_path`` — относительный
    путь файла плагина внутри :class:`PluginWorkspaceShell` (для логов
    и сообщений; реальный путь на диске наружу не уходит).
    """

    def __init__(self, rel_path: str, message: str) -> None:
        super().__init__(f"plugin {rel_path!r}: {message}")
        self.rel_path = rel_path


class PluginLoadError(PluginError):
    """Ошибка discovery: чтение, компиляция или ``exec`` плагин-файла
    провалились, либо в модуле нет callable-``register``. Loader ловит
    эту ошибку, пишет warning и продолжает с остальными файлами —
    один битый плагин не валит весь старт.
    """


class PluginRegisterError(PluginError):
    """Ошибка инстанцирования: ``register(ctx)`` бросил исключение или
    вернул неожиданный объект. Loader ловит эту ошибку, пишет warning
    и пропускает плагин на текущем запросе — остальные плагины
    собираются в ToolsService.
    """


@dataclass(frozen=True)
class PluginContext:
    """Контракт окружения, передаваемый плагину при инстанцировании tool'ов.

    Per-request: ``project_workspace`` создаётся заново на каждое
    сообщение из user-session id и используется tool'ами для I/O в
    проект пользователя. Application-singletons: всё остальное — одно
    на процесс, шарится между запросами.

    LLM-credentials намеренно НЕ передаются: плагины — full-trust код
    (см. trust-модель в :class:`PluginLoader`), но даже в trust-модели
    нет смысла раздавать ``api_key`` каждому плагину «на всякий случай».
    Когда появится плагин, которому нужен LLM, дадим ему узкий
    LLM-клиент-фасад без credentials, а не :class:`LLMConfig` целиком.
    """

    project_workspace: ProjectWorkspaceShell
    plugin_workspace: PluginWorkspaceShell
    app_config: AppConfig
    agent_config: AgentConfig
    sampling: SamplingParams


class PluginLoader:
    """Discovery .py-плагинов из :class:`PluginWorkspaceShell` и сборка
    :class:`ToolsService` на каждый запрос.

    Discovery — один раз в конструкторе: I/O идёт через ``WorkspaceShell``,
    физические пути не утекают. Импорт через ``exec(compile(...))`` в
    свежий :class:`ModuleType` — без относительных импортов внутри
    плагина и без регистрации в ``sys.modules``; для одно-файловых
    плагинов это нормальный компромисс.

    **Trust-модель.** Плагины исполняются как полноценный Python-код в
    адресном пространстве процесса (``exec``) — это RCE по построению,
    как и любой пакет, установленный через ``pip``. Изоляции нет:
    плагин видит весь ``sys.modules``, может работать с файловой
    системой в обход :class:`WorkspaceShell`, открывать сетевые
    соединения, запускать процессы. ``WorkspaceShell``-абстракция —
    convention для нашего кода, не security boundary.

    Из этого следует операционный контракт:

    1. Плагины — это код, не пользовательский ввод. Поставляются
       только через trusted-канал (CI с code-review/подписями), как и
       зависимости приложения.
    2. ``plugins_dir`` обязан быть read-only в runtime: writable
       directory = locally-unprivileged эскалация в RCE при
       перезапуске. Контролируется на уровне deploy (file mode,
       read-only mount, ownership).
    3. :class:`PluginContext` намеренно урезан и не содержит
       credentials (``api_key``, токены) — даже в full-trust модели нет
       причин раздавать секреты каждому плагину «на всякий случай».
    """

    def __init__(self, workspace: PluginWorkspaceShell) -> None:
        self._workspace = workspace
        self._registers: list[tuple[str, PluginRegisterFn]] = []
        self._discover()

    def plugin_count(self) -> int:
        """Количество загруженных плагинов — для smoke-тестов и логов."""
        return len(self._registers)

    def build_tools_service(self, ctx: PluginContext) -> ToolsService:
        """Per-request сборка :class:`ToolsService` через
        ``register(ctx)`` всех загруженных плагинов.

        :class:`PluginRegisterError` (исключение в ``register`` или
        некорректный возврат) логируется и пропускается; остальные
        плагины подключаются. Низкоуровневые исключения из тела
        ``register`` оборачиваются в ``PluginRegisterError`` —
        наружу из loader они не уходят.
        """
        factory = ToolFactory()
        for rel_path, register in self._registers:
            try:
                self._apply_register(rel_path, register, ctx, factory)
            except PluginRegisterError as e:
                logger.warning("%s; skipped this request", e)
        service = ToolsService(factory)
        service.rebuild_catalog()
        return service

    @staticmethod
    def _apply_register(
        rel_path: str,
        register: PluginRegisterFn,
        ctx: PluginContext,
        factory: ToolFactory,
    ) -> None:
        try:
            sources = register(ctx)
            for source in sources:
                factory.register(source)
        except PluginError:
            raise
        except Exception as e:
            raise PluginRegisterError(
                rel_path, f"register(ctx) failed: {type(e).__name__}: {e}"
            ) from e

    def _discover(self) -> None:
        for rel_path in self._workspace.tree():
            if not self._is_plugin_file(rel_path):
                continue
            try:
                self._load_module(rel_path)
            except PluginLoadError as e:
                logger.warning("%s; skipped", e)

    @staticmethod
    def _is_plugin_file(rel_path: str) -> bool:
        if not rel_path.endswith(".py"):
            return False
        name = rel_path.rsplit("/", 1)[-1]
        return not name.startswith("_")

    def _load_module(self, rel_path: str) -> None:
        try:
            with self._workspace.read_text(rel_path) as f:
                source = f.read()
        except WorkspaceError as e:
            raise PluginLoadError(rel_path, f"read failed: {e}") from e

        # Валидное Python-имя модуля: "tools/files/cat.py" -> "tools.files.cat".
        # Без этого ``typing.get_type_hints`` (который дёргается из
        # ``@dataclass``-декоратора и подобных) пытается достать
        # ``sys.modules[cls.__module__]``, не находит и падает с
        # AttributeError на ``None.__dict__``.
        module_name = "boba.plugins." + (
            rel_path.removesuffix(".py").replace("/", ".")
        )
        module = ModuleType(module_name)
        # __file__ нужен для понятных трейсбеков; реальный физический путь
        # наружу не показываем — relative path внутри workspace и так
        # уникален для diagnostics.
        module.__dict__["__file__"] = rel_path
        try:
            code = compile(source, rel_path, "exec")
        except SyntaxError as e:
            raise PluginLoadError(rel_path, f"syntax error: {e}") from e

        # Регистрируем модуль в sys.modules ДО exec — иначе
        # ``typing.get_type_hints`` не сможет резолвить аннотации внутри
        # ``@dataclass``/``@dataclass(frozen=True)``. На failure снимаем
        # запись, чтобы битый плагин не висел в namespace.
        sys.modules[module_name] = module
        try:
            exec(code, module.__dict__)  # noqa: S102
        except Exception as e:
            sys.modules.pop(module_name, None)
            raise PluginLoadError(
                rel_path, f"exec failed: {type(e).__name__}: {e}"
            ) from e

        register = module.__dict__.get("register")
        if not callable(register):
            sys.modules.pop(module_name, None)
            raise PluginLoadError(
                rel_path, "missing or non-callable `register(ctx)`"
            )
        self._registers.append((rel_path, cast(PluginRegisterFn, register)))
