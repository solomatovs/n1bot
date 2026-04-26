"""Конкретные реализации :class:`PromptProvider` (system-prompt).

Все провайдеры пишут в system-prompt: USER-сообщение в boba не
собирается через :class:`PromptFactory` — оно приходит готовым в
:attr:`AgentRequest.query` от caller'а (frontend/CLI отвечает за
обогащение IDE-selection / шаблонами / контекстом вызова).

Набор провайдеров:

- ``StaticPromptProvider`` — фиксированный текст из DI.
- ``FilePromptProvider`` — читает блок с диска.
- ``EnvironmentPromptProvider`` — информация о среде выполнения.
- ``GitPromptProvider`` — состояние git-репо.
- ``WorkspaceSystemPromptProvider`` — читает все файлы директории
  внутри :class:`HistoryWorkspaceShell` (workspace-слой живёт в
  shared ``boba.domain.core.workspace``).

Пакет — отдельный pip-package; основной ``boba`` от него НЕ зависит.
Короткие импорты::

    from boba_adapter_prompt_providers import (
        StaticPromptProvider,
        FilePromptProvider,
        EnvironmentPromptProvider,
        GitPromptProvider,
        WorkspaceSystemPromptProvider,
    )
"""

from __future__ import annotations

import os
import platform
import subprocess
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from boba.domain.agent.models import AgentContext
from boba.domain.agent.prompt import (
    PromptBlock,
    PromptId,
    PromptProvider,
    PromptState,
)
from boba.domain.core.workspace import HistoryWorkspaceShell


class StaticPromptProvider(PromptProvider):
    """Фиксированный текст, зашитый в конфигурацию DI."""

    def __init__(
        self,
        prompt_id: PromptId,
        priority: int,
        content: str,
    ) -> None:
        self._id = prompt_id
        self._priority = priority
        self._content = content

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

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
        default_prompt: str = "",
    ) -> None:
        self._id = prompt_id
        self._priority = priority
        self._path = path
        self._default_prompt = default_prompt

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

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

    def blocks(self, state: PromptState[AgentContext]) -> Iterable[PromptBlock]:
        branch = self._git("branch", "--show-current")
        status = self._git("status", "--short")
        log = self._git("log", "--oneline", "-5")
        content = (
            f"Current branch: {branch}\n\nStatus:\n{status}\n\nRecent commits:\n{log}"
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


class WorkspaceSystemPromptProvider(PromptProvider):
    """
    Собирает system-промпт из файлов внутри директории workspace'а.
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

    def blocks(self, state: PromptState[AgentContext]) -> Iterator[PromptBlock]:
        for path in sorted(self._workspace.ls(self._directory)):
            with self._workspace.read_text(path) as f:
                content = f.read().strip()
            if content:
                yield PromptBlock(name=path, content=content)
