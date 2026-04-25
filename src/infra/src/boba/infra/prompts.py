"""Prompt-инфраструктура: discovery .md/.txt-блоков и .py-плагинов
из prompt workspace и сборка списка :class:`PromptProvider`.

Структура prompt-workspace:

* ``system/*.md`` (или ``.txt``) → :class:`StaticPromptProvider` с
  ``kind=PromptKind.SYSTEM``.
* ``user/*.md`` (или ``.txt``) → то же, ``kind=PromptKind.USER``.
* ``*.py`` (где угодно в дереве, не начиная с ``_``) — full-trust
  prompt-плагин, экспортирующий
  ``register(ctx: PromptContext) -> Iterable[PromptProvider]``.

Имя текстового файла может начинаться с префикса ``NN-``
(например, ``00-identity.md``) — число становится ``priority``
соответствующего :class:`StaticPromptProvider`; без префикса
используется дефолт ``100``. У .py-плагинов приоритет назначает сам
provider через :meth:`PromptProvider.priority`.

Любой сегмент пути, начинающийся с ``_``, игнорируется (по аналогии
с ``__pycache__`` и приватными файлами).

Жизненный цикл: :class:`PromptLoader` создаётся один раз на старте,
делает discovery в конструкторе и кэширует список провайдеров. На
запрос отдаётся кэш — без повторного I/O. Сбой одного файла даёт
warning + skip; остальные грузятся.

Trust-модель .py-плагинов та же, что у :class:`PluginLoader`:
исполнение ``exec`` без изоляции — поставлять только через trusted-
канал, ``prompts_dir`` обязан быть read-only в runtime.
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
from boba.domain.agent.prompt import PromptId, PromptKind, PromptProvider
from boba.domain.config import AppConfig
from boba.domain.core.workspace import PromptWorkspaceShell, WorkspaceError

logger = logging.getLogger(__name__)


PromptRegisterFn = Callable[["PromptContext"], Iterable[PromptProvider]]


class PromptError(Exception):
    """Базовая ошибка prompt-инфры. Несёт ``rel_path`` — относительный
    путь файла внутри :class:`PromptWorkspaceShell`.
    """

    def __init__(self, rel_path: str, message: str) -> None:
        super().__init__(f"prompt {rel_path!r}: {message}")
        self.rel_path = rel_path


class PromptLoadError(PromptError):
    """Ошибка discovery: чтение файла, ``compile`` или ``exec`` плагин-
    файла провалились, либо в модуле нет callable-``register``.
    """


class PromptRegisterError(PromptError):
    """Ошибка инстанцирования: ``register(ctx)`` бросил исключение."""


@dataclass(frozen=True)
class PromptContext:
    """Контракт окружения для .py prompt-плагинов.

    ``prompt_workspace`` даёт плагину доступ к соседним файлам в
    директории промптов (например, читать общие фрагменты).
    ``app_config`` — для редких случаев, когда плагину нужны базовые
    настройки приложения.
    """

    prompt_workspace: PromptWorkspaceShell
    app_config: AppConfig


_KIND_DIRS: dict[str, PromptKind] = {
    "system": PromptKind.SYSTEM,
    "user": PromptKind.USER,
}

_PRIORITY_PREFIX_RE = re.compile(r"^(\d+)-")
_DEFAULT_PRIORITY = 100
_TEXT_EXTENSIONS = (".md", ".txt")


class PromptLoader:
    """Discovery промптов и prompt-плагинов из
    :class:`PromptWorkspaceShell`.

    Конструктор делает discovery один раз: проходит ``workspace.tree()``,
    разводит файлы по типу (текст/плагин/иное) и собирает список
    :class:`PromptProvider`. Метод :meth:`providers` возвращает
    закэшированный результат.
    """

    def __init__(
        self,
        workspace: PromptWorkspaceShell,
        app_config: AppConfig,
    ) -> None:
        self._workspace = workspace
        self._ctx = PromptContext(
            prompt_workspace=workspace,
            app_config=app_config,
        )
        self._providers: list[PromptProvider] = []
        self._discover()

    def providers(self) -> Sequence[PromptProvider]:
        """Закэшированный список провайдеров — для AgentComponents."""
        return tuple(self._providers)

    def prompt_count(self) -> int:
        """Количество загруженных провайдеров — для smoke-тестов и логов."""
        return len(self._providers)

    def _discover(self) -> None:
        for rel_path in self._workspace.tree():
            if any(seg.startswith("_") for seg in rel_path.split("/")):
                continue
            try:
                self._dispatch(rel_path)
            except PromptError as e:
                logger.warning("%s; skipped", e)

    def _dispatch(self, rel_path: str) -> None:
        if rel_path.endswith(_TEXT_EXTENSIONS):
            self._load_text(rel_path)
        elif rel_path.endswith(".py"):
            self._load_module(rel_path)
        # Незнакомые расширения игнорируются молча.

    def _load_text(self, rel_path: str) -> None:
        kind = self._kind_for(rel_path)
        if kind is None:
            logger.warning(
                "prompt %r: text file outside system/|user/; skipped", rel_path
            )
            return
        try:
            with self._workspace.read_text(rel_path) as f:
                content = f.read()
        except WorkspaceError as e:
            raise PromptLoadError(rel_path, f"read failed: {e}") from e
        if not content.strip():
            logger.info("prompt %r: empty content; skipped", rel_path)
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
                kind=kind,
            )
        )

    @staticmethod
    def _kind_for(rel_path: str) -> PromptKind | None:
        first_segment = rel_path.split("/", 1)[0]
        return _KIND_DIRS.get(first_segment)

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
            raise PromptLoadError(rel_path, f"read failed: {e}") from e

        # Валидное Python-имя: "system/foo.py" -> "system.foo"
        module_name = "boba.prompts." + (
            rel_path.removesuffix(".py").replace("/", ".")
        )
        module = ModuleType(module_name)
        module.__dict__["__file__"] = rel_path
        try:
            code = compile(source, rel_path, "exec")
        except SyntaxError as e:
            raise PromptLoadError(rel_path, f"syntax error: {e}") from e

        # sys.modules-регистрация ДО exec — чтобы typing.get_type_hints
        # внутри @dataclass и пр. видел модуль.
        sys.modules[module_name] = module
        try:
            exec(code, module.__dict__)  # noqa: S102
        except Exception as e:
            sys.modules.pop(module_name, None)
            raise PromptLoadError(
                rel_path, f"exec failed: {type(e).__name__}: {e}"
            ) from e

        register = module.__dict__.get("register")
        if not callable(register):
            sys.modules.pop(module_name, None)
            raise PromptLoadError(
                rel_path, "missing or non-callable `register(ctx)`"
            )
        try:
            for provider in cast(PromptRegisterFn, register)(self._ctx):
                self._providers.append(provider)
        except Exception as e:
            raise PromptRegisterError(
                rel_path, f"register(ctx) failed: {type(e).__name__}: {e}"
            ) from e
