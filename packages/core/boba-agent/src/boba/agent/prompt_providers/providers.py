"""Реализации PromptProvider (system-prompt)."""

from __future__ import annotations

import os
import platform
import subprocess
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from boba.agent.prompt import (
    PromptBlock,
    PromptId,
    PromptProvider,
    PromptState,
)
from boba.workspace.contract import HistoryWorkspaceShell


class FilePromptProvider(PromptProvider):
    """Читает блок промпта из файла; отсутствующий файл → default_prompt."""

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

    def blocks(self, state: PromptState) -> Iterable[PromptBlock]:
        if self._path.exists():
            content = self._path.read_text(encoding="utf-8")
        else:
            content = self._default_prompt
        yield PromptBlock(name=self._id, content=content)


class EnvironmentPromptProvider(PromptProvider):
    """Блок с информацией о среде выполнения (платформа, shell, ОС, дата)."""

    def __init__(self, priority: int = 60) -> None:
        self._id = PromptId("environment")
        self._priority = priority

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def blocks(self, state: PromptState) -> Iterable[PromptBlock]:
        lines = [
            f"Platform: {platform.system()}",
            f"Shell: {os.environ.get('SHELL', 'unknown')}",
            f"OS Version: {platform.release()}",
            f"Current date: {datetime.now(UTC).date().isoformat()}",
        ]
        yield PromptBlock(name=self._id, content="\n".join(lines))


class GitPromptProvider(PromptProvider):
    """Блок с текущим состоянием git-репозитория."""

    _GIT_TIMEOUT_SECONDS = 5

    def __init__(self, priority: int = 80) -> None:
        self._id = PromptId("git_status")
        self._priority = priority

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def blocks(self, state: PromptState) -> Iterable[PromptBlock]:
        branch = self._git("branch", "--show-current")
        status = self._git("status", "--short")
        log = self._git("log", "--oneline", "-5")
        content = (
            f"Current branch: {branch}\n\nStatus:\n{status}\n\nRecent commits:\n{log}"
        )
        yield PromptBlock(name=self._id, content=content)

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


class DirectoryPromptProvider(PromptProvider):
    """Читает все файлы из директории; каждый файл — отдельный `PromptBlock`.

    После 1:1-маппинга `PromptBlock` → `SystemMessage` в SystemPromptReducer
    несколько файлов разворачиваются в несколько system-сообщений, что
    1-в-1 ложится на Anthropic multi-block system. Несуществующая
    директория молча даёт ноль блоков (как `FilePromptProvider` с default).
    """

    def __init__(
        self,
        prompt_id: PromptId,
        priority: int,
        directory: Path,
        *,
        glob: str = "*",
    ) -> None:
        self._id = prompt_id
        self._priority = priority
        self._directory = directory
        self._glob = glob

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def blocks(self, state: PromptState) -> Iterator[PromptBlock]:
        if not self._directory.is_dir():
            return
        for path in sorted(self._directory.glob(self._glob)):
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8").strip()
            if content:
                yield PromptBlock(name=str(path), content=content)


class WorkspaceSystemPromptProvider(PromptProvider):
    """Собирает system-промпт из файлов директории workspace'а."""

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

    def blocks(self, state: PromptState) -> Iterator[PromptBlock]:
        for path in sorted(self._workspace.ls(self._directory)):
            with self._workspace.read_text(path) as f:
                content = f.read().strip()
            if content:
                yield PromptBlock(name=path, content=content)
