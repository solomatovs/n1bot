"""Extension-инфраструктура: единый loader для tools и prompts.

Структура папок внутри ``BOBA_EXTENSIONS_DIR`` свободная — loader
рекурсивно обходит дерево и обрабатывает каждый файл по его
расширению:

* ``*.py`` — full-trust плагин; экспортирует одну или обе функции:
  ``register_tools(ctx) -> Iterable[ToolSource]`` и
  ``register_prompts(ctx) -> Iterable[PromptProvider]``. Файл без
  обеих — warning + skip.
* ``*.md`` / ``*.txt`` — становится :class:`StaticPromptProvider`
  (system-prompt блок). Priority извлекается из ``\\d+-`` префикса
  в имени файла; пустые файлы — skip.
* любое другое расширение — silently skip.
* любой сегмент пути с ``_*`` префиксом — silently skip (как
  ``__pycache__``, ``_draft.md``, ``_helpers.py``).

Жизненный цикл: :class:`ExtensionLoader` создаётся **один раз** на
старте процесса. В конструкторе делается discovery, exec-импорт всех
``.py``, вызовы ``register_tools``/``register_prompts``, чтение всех
текстовых файлов, и сборка двух кэшей: :class:`ToolsService` и
``Sequence[PromptProvider]``. Дальше ничего не пересобирается:
``loader.tools_service()`` и ``loader.prompt_providers()`` отдают
кэшированные инстансы.

Trust-модель та же, что у предыдущего ``PluginLoader``:

1. Расширения — это код, не пользовательский ввод. Поставляются
   через trusted-канал (CI с code-review/подписями).
2. ``extensions_dir`` обязан быть read-only в runtime — writable
   директория = locally-unprivileged эскалация в RCE при
   перезапуске. Контролируется на уровне deploy.
3. :class:`ExtensionContext` намеренно урезан: не содержит
   credentials (``api_key``, токены) — даже в full-trust модели.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import cast

from boba.adapters.prompt_providers import StaticPromptProvider
from boba.domain.agent.models import AgentConfig
from boba.domain.agent.prompt import PromptId, PromptProvider
from boba.domain.config import AppConfig
from boba.domain.core.tools import ToolFactory, ToolSource, ToolsService
from boba.domain.core.workspace import ExtensionWorkspaceShell, WorkspaceError

logger = logging.getLogger(__name__)


RegisterToolsFn = Callable[["ExtensionContext"], Iterable[ToolSource]]
RegisterPromptsFn = Callable[["ExtensionContext"], Iterable[PromptProvider]]


class ExtensionError(Exception):
    """Базовая ошибка extension-инфры. Несёт ``rel_path`` —
    относительный путь файла внутри :class:`ExtensionWorkspaceShell`.
    """

    def __init__(self, rel_path: str, message: str) -> None:
        super().__init__(f"extension {rel_path!r}: {message}")
        self.rel_path = rel_path


class ExtensionLoadError(ExtensionError):
    """Ошибка discovery: чтение файла, ``compile`` или ``exec`` плагина
    провалились, либо в ``.py``-модуле нет ни ``register_tools``, ни
    ``register_prompts``.
    """


class ExtensionRegisterError(ExtensionError):
    """Ошибка инстанцирования: ``register_tools(ctx)`` или
    ``register_prompts(ctx)`` бросил исключение или вернул что-то
    некорректное.
    """


@dataclass(frozen=True)
class ExtensionContext:
    """Контракт окружения для ``register_tools``/``register_prompts``.

    Все поля — application-singletons: ``ExtensionContext`` строится
    один раз на старте, передаётся в register-вызовы каждого плагина,
    дальше не меняется. Per-request зависимости (project_workspace
    пользователя) сюда НЕ входят: tool-инстансы получают рабочий
    workspace через :class:`~boba.domain.core.tools.ToolContext` в
    :meth:`Tool.execute`, а prompt-провайдеры — через ``state.ctx`` в
    :meth:`PromptProvider.blocks`.

    LLM-credentials также не передаются — даже в full-trust модели
    нет причин раздавать ``api_key`` каждому плагину «на всякий
    случай».
    """

    extension_workspace: ExtensionWorkspaceShell
    app_config: AppConfig
    agent_config: AgentConfig


_PRIORITY_PREFIX_RE = re.compile(r"^(\d+)-")
_DEFAULT_PRIORITY = 100
_TEXT_EXTENSIONS = (".md", ".txt")


class ExtensionLoader:
    """Discovery .py/.md/.txt из :class:`ExtensionWorkspaceShell` и
    сборка :class:`ToolsService` + ``Sequence[PromptProvider]`` один
    раз на старте процесса.

    Discovery + register-вызовы происходят в конструкторе; результаты
    кэшируются. ``tools_service()``/``prompt_providers()`` — просто
    отдают закэшированный инстанс.
    """

    def __init__(
        self,
        workspace: ExtensionWorkspaceShell,
        ctx: ExtensionContext,
    ) -> None:
        self._workspace = workspace
        self._ctx = ctx
        self._tool_sources: list[ToolSource] = []
        self._providers: list[PromptProvider] = []
        self._discover()
        self._tools_service = self._build_tools_service()

    def tools_service(self) -> ToolsService:
        """Закэшированный :class:`ToolsService` со всеми ``register_tools``."""
        return self._tools_service

    def prompt_providers(self) -> Sequence[PromptProvider]:
        """Закэшированный список :class:`PromptProvider` —
        ``StaticPromptProvider`` из текстовых файлов плюс динамические
        из ``register_prompts``.
        """
        return tuple(self._providers)

    def _build_tools_service(self) -> ToolsService:
        factory = ToolFactory()
        for source in self._tool_sources:
            factory.register(source)
        service = ToolsService(factory)
        service.rebuild_catalog()
        return service

    def _discover(self) -> None:
        for rel_path in self._workspace.tree():
            if any(seg.startswith("_") for seg in rel_path.split("/")):
                continue
            try:
                self._dispatch(rel_path)
            except ExtensionError as e:
                logger.warning("%s; skipped", e)

    def _dispatch(self, rel_path: str) -> None:
        if rel_path.endswith(_TEXT_EXTENSIONS):
            self._load_text(rel_path)
        elif rel_path.endswith(".py"):
            self._load_module(rel_path)
        # Незнакомые расширения — silently skip.

    def _load_text(self, rel_path: str) -> None:
        try:
            with self._workspace.read_text(rel_path) as f:
                content = f.read()
        except WorkspaceError as e:
            raise ExtensionLoadError(rel_path, f"read failed: {e}") from e
        if not content.strip():
            logger.info("extension %r: empty content; skipped", rel_path)
            return
        priority = self._priority_for(rel_path)
        prompt_id = PromptId(
            rel_path.removesuffix(".md").removesuffix(".txt")
        )
        self._providers.append(
            StaticPromptProvider(
                prompt_id=prompt_id,
                priority=priority,
                content=content.rstrip("\n"),
            )
        )

    @staticmethod
    def _priority_for(rel_path: str) -> int:
        name = rel_path.rsplit("/", 1)[-1]
        match = _PRIORITY_PREFIX_RE.match(name)
        if match:
            return int(match.group(1))
        return _DEFAULT_PRIORITY

    def _load_module(self, rel_path: str) -> None:
        try:
            with self._workspace.read_text(rel_path) as f:
                source = f.read()
        except WorkspaceError as e:
            raise ExtensionLoadError(rel_path, f"read failed: {e}") from e

        # Валидное Python-имя: "files/cat.py" -> "boba.extensions.files.cat"
        module_name = "boba.extensions." + (
            rel_path.removesuffix(".py").replace("/", ".")
        )
        module = ModuleType(module_name)
        # __file__ нужен для понятных трейсбеков; реальный физический
        # путь наружу не показываем — relative path внутри workspace и
        # так уникален для diagnostics.
        module.__dict__["__file__"] = rel_path
        try:
            code = compile(source, rel_path, "exec")
        except SyntaxError as e:
            raise ExtensionLoadError(rel_path, f"syntax error: {e}") from e

        # sys.modules-регистрация ДО exec — иначе typing.get_type_hints
        # внутри @dataclass и пр. не сможет резолвить аннотации.
        sys.modules[module_name] = module
        try:
            exec(code, module.__dict__)  # noqa: S102
        except Exception as e:
            sys.modules.pop(module_name, None)
            raise ExtensionLoadError(
                rel_path, f"exec failed: {type(e).__name__}: {e}"
            ) from e

        register_tools = module.__dict__.get("register_tools")
        register_prompts = module.__dict__.get("register_prompts")

        if not callable(register_tools) and not callable(register_prompts):
            sys.modules.pop(module_name, None)
            raise ExtensionLoadError(
                rel_path,
                "missing both `register_tools(ctx)` and `register_prompts(ctx)`",
            )

        if callable(register_tools):
            self._apply_register_tools(
                rel_path, cast(RegisterToolsFn, register_tools)
            )
        if callable(register_prompts):
            self._apply_register_prompts(
                rel_path, cast(RegisterPromptsFn, register_prompts)
            )

    def _apply_register_tools(
        self,
        rel_path: str,
        register: RegisterToolsFn,
    ) -> None:
        try:
            for source in register(self._ctx):
                self._tool_sources.append(source)
        except ExtensionError:
            raise
        except Exception as e:
            raise ExtensionRegisterError(
                rel_path,
                f"register_tools(ctx) failed: {type(e).__name__}: {e}",
            ) from e

    def _apply_register_prompts(
        self,
        rel_path: str,
        register: RegisterPromptsFn,
    ) -> None:
        try:
            for provider in register(self._ctx):
                self._providers.append(provider)
        except ExtensionError:
            raise
        except Exception as e:
            raise ExtensionRegisterError(
                rel_path,
                f"register_prompts(ctx) failed: {type(e).__name__}: {e}",
            ) from e
