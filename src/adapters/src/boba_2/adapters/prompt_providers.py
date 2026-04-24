"""Конкретные реализации :class:`PromptProvider`.

Набор провайдеров:

- ``StaticPromptProvider`` — фиксированный текст из DI.
- ``FilePromptProvider`` — читает блок с диска.
- ``EnvironmentPromptProvider`` — информация о среде выполнения.
- ``GitPromptProvider`` — состояние git-репо.
- ``UserQueryProvider`` — query из :class:`AgentContext`.
- ``IDESelectionProvider`` — выделенный код из IDE.
- ``TemplateProvider`` — шаблон с подстановкой ``{query}``.
- ``WorkspaceSystemPromptProvider`` — читает все файлы директории
  внутри :class:`HistoryWorkspaceShell` (workspace-слой живёт в
  shared ``boba.domain.core.workspace``).
"""

from __future__ import annotations

import os
import platform
import subprocess
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from boba.domain.core.workspace import HistoryWorkspaceShell
from boba_2.domain.agent.models import AgentContext
from boba_2.domain.agent.prompt import (
    PromptBlock,
    PromptId,
    PromptKind,
    PromptProvider,
    PromptState,
)


class StaticPromptProvider(PromptProvider):
    """Фиксированный текст, зашитый в конфигурацию DI."""

    def __init__(
        self,
        prompt_id: PromptId,
        priority: int,
        content: str,
        kind: PromptKind,
    ) -> None:
        self._id = prompt_id
        self._priority = priority
        self._content = content
        self._kind = kind

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def kind(self) -> PromptKind:
        return self._kind

    def blocks(self, state: PromptState[AgentContext]) -> Iterable[PromptBlock]:
        yield PromptBlock(name=self._id.name, content=self._content)


class FilePromptProvider(PromptProvider):
    """Читает блок промпта из файла на диске.

    Отсутствующий файл → ``default_prompt`` (по умолчанию — пусто).
    """

    def __init__(
        self,
        prompt_id: PromptId,
        priority: int,
        path: Path,
        kind: PromptKind,
        default_prompt: str = "",
    ) -> None:
        self._id = prompt_id
        self._priority = priority
        self._path = path
        self._kind = kind
        self._default_prompt = default_prompt

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def kind(self) -> PromptKind:
        return self._kind

    def blocks(self, state: PromptState[AgentContext]) -> Iterable[PromptBlock]:
        if self._path.exists():
            content = self._path.read_text(encoding="utf-8")
        else:
            content = self._default_prompt
        yield PromptBlock(name=self._id.name, content=content)


class EnvironmentPromptProvider(PromptProvider):
    """Блок с информацией о среде выполнения.

    Включает платформу, shell, версию ОС и текущую дату — полезно для
    моделей, у которых нет access к этим данным из обучения.
    """

    def __init__(self, priority: int = 60) -> None:
        self._id = PromptId("environment")
        self._priority = priority

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def kind(self) -> PromptKind:
        return PromptKind.SYSTEM

    def blocks(self, state: PromptState[AgentContext]) -> Iterable[PromptBlock]:
        lines = [
            f"Platform: {platform.system()}",
            f"Shell: {os.environ.get('SHELL', 'unknown')}",
            f"OS Version: {platform.release()}",
            f"Current date: {datetime.now(UTC).date().isoformat()}",
        ]
        yield PromptBlock(name=self._id.name, content="\n".join(lines))


class GitPromptProvider(PromptProvider):
    """Блок с текущим состоянием git-репозитория.

    Вне репозитория / при отсутствии git — ``"(unavailable)"`` в
    соответствующих полях, но блок всё равно эмитится (решение
    принято старым кодом, переносим семантику).
    """

    _GIT_TIMEOUT_SECONDS = 5

    def __init__(self, priority: int = 80) -> None:
        self._id = PromptId("git_status")
        self._priority = priority

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def kind(self) -> PromptKind:
        return PromptKind.SYSTEM

    def blocks(self, state: PromptState[AgentContext]) -> Iterable[PromptBlock]:
        branch = self._git("branch", "--show-current")
        status = self._git("status", "--short")
        log = self._git("log", "--oneline", "-5")
        content = (
            f"Current branch: {branch}\n\n"
            f"Status:\n{status}\n\n"
            f"Recent commits:\n{log}"
        )
        yield PromptBlock(name=self._id.name, content=content)

    @classmethod
    def _git(cls, *args: str) -> str:
        try:
            result = subprocess.run(
                args=args,
                executable="git",
                capture_output=True,
                text=True,
                check=False,
                timeout=cls._GIT_TIMEOUT_SECONDS,
            )
            return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return "(unavailable)"


class UserQueryProvider(PromptProvider):
    """Query пользователя из :class:`AgentContext.agent_request.query`.

    Самый частый user-провайдер: ставится первым в цепочке user-блоков,
    дальше могут идти template/context-провайдеры, оборачивающие
    / дополняющие query.
    """

    def __init__(self, priority: int = 50) -> None:
        self._id = PromptId("user_query")
        self._priority = priority

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def kind(self) -> PromptKind:
        return PromptKind.USER

    def blocks(self, state: PromptState[AgentContext]) -> Iterable[PromptBlock]:
        yield PromptBlock(
            name=self._id.name,
            content=state.ctx.agent_request.query,
        )


class IDESelectionProvider(PromptProvider):
    """Контекст выделенных строк из IDE (файл + блок кода)."""

    def __init__(
        self,
        file_path: str,
        selection: str,
        priority: int = 30,
    ) -> None:
        self._id = PromptId("ide_selection")
        self._file_path = file_path
        self._selection = selection
        self._priority = priority

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def kind(self) -> PromptKind:
        return PromptKind.USER

    def blocks(self, state: PromptState[AgentContext]) -> Iterable[PromptBlock]:
        content = (
            f"Selected code from {self._file_path}:\n"
            f"```\n{self._selection}\n```"
        )
        yield PromptBlock(name=self._id.name, content=content)


class TemplateProvider(PromptProvider):
    """Шаблон с подстановкой ``{query}`` из ``AgentContext``.

    Удобен, когда нужно обернуть query инструкцией (например,
    «Answer in Russian: {query}») без написания отдельного провайдера.
    """

    def __init__(
        self,
        prompt_id: PromptId,
        priority: int,
        template: str,
        kind: PromptKind,
    ) -> None:
        self._id = prompt_id
        self._priority = priority
        self._template = template
        self._kind = kind

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def kind(self) -> PromptKind:
        return self._kind

    def blocks(self, state: PromptState[AgentContext]) -> Iterable[PromptBlock]:
        content = self._template.format(query=state.ctx.agent_request.query)
        yield PromptBlock(name=self._id.name, content=content)


class WorkspaceSystemPromptProvider(PromptProvider):
    """Собирает system-промпт из файлов внутри директории workspace'а.

    Каждый непустой файл в указанной поддиректории становится
    отдельным :class:`PromptBlock`. Файлы читаются в
    лексикографическом порядке имён. Отсутствующая директория или
    пустой набор непустых файлов → пустой итератор (на выходе
    фабрики — просто нет вклада этого провайдера).

    Использует абстрактный :class:`HistoryWorkspaceShell` из
    shared ``boba.domain.core.workspace`` — сам workspace-слой в
    ``boba_2`` не копируется, провайдер работает над любой
    совместимой реализацией (``FsHistoryWorkspaceShell`` из
    прода, mock / in-memory в тестах).

    I/O-ошибки от workspace (``WorkspaceError`` / ``OSError``)
    **не** глушатся здесь: их ловит :class:`PromptFactory.build` и
    оборачивает в :class:`PromptProviderError` → :class:`PromptFailed`
    через стандартную маршрутизацию.
    """

    def __init__(
        self,
        prompt_id: PromptId,
        priority: int,
        workspace: HistoryWorkspaceShell,
        directory: str,
    ) -> None:
        self._id = prompt_id
        self._priority = priority
        self._workspace = workspace
        self._directory = directory

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def kind(self) -> PromptKind:
        return PromptKind.SYSTEM

    def blocks(self, state: PromptState[AgentContext]) -> Iterator[PromptBlock]:
        for path in sorted(self._workspace.ls(self._directory)):
            with self._workspace.read_text(path) as f:
                content = f.read().strip()
            if content:
                yield PromptBlock(name=path, content=content)
