"""Конкретные реализации PromptProvider."""

from __future__ import annotations

import os
import platform
import subprocess
from collections.abc import Iterable, Iterator
from datetime import date
from pathlib import Path

from boba.domain.agent.meat import AgentContext
from boba.domain.core.promt import (
    PromptBlock,
    PromptId,
    PromptProvider,
    PromtState,
)
from boba.domain.core.workspace import WorkspaceService


class StaticPromptProvider(PromptProvider):
    """Фиксированный текст, зашитый в код."""

    def __init__(self, prompt_id: PromptId, priority: int, content: str) -> None:
        self._id = prompt_id
        self._priority = priority
        self._content = content

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def blocks(self, state: PromtState[AgentContext]) -> Iterable[PromptBlock]:
        yield PromptBlock(name=self._id.name, content=self._content)


class FilePromptProvider(PromptProvider):
    """Читает блок из файла на диске."""

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

    def blocks(self, state: PromtState[AgentContext]) -> Iterable[PromptBlock]:
        if self._path.exists():
            content = self._path.read_text(encoding="utf-8")
        else:
            content = self._default_prompt

        yield PromptBlock(name=self._id.name, content=content)


class WorkspaceSystemPromptProvider(PromptProvider):
    """Собирает системный промт из файлов внутри директории workspace'а.

    Каждый непустой файл отдаётся отдельным ``PromptBlock``. Файлы читаются
    в лексикографическом порядке имён. Директория по умолчанию —
    ``prompts/system``. Отсутствующая директория или пустой набор файлов →
    пустой итератор.
    """

    def __init__(
        self,
        prompt_id: PromptId,
        priority: int,
        workspace: WorkspaceService,
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

    def blocks(self, state: PromtState[AgentContext]) -> Iterator[PromptBlock]:
        for path in sorted(self._workspace.ls(self._directory)):
            with self._workspace.read_text(path) as f:
                content = f.read().strip()
            if content:
                yield PromptBlock(name=path, content=content)


class EnvironmentPromptProvider(PromptProvider):
    """Информация о среде выполнения."""

    def __init__(self) -> None:
        self._id = PromptId("environment")

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return 60

    def blocks(self, state: PromtState[AgentContext]) -> Iterable[PromptBlock]:
        lines = [
            f"Platform: {platform.system()}",
            f"Shell: {os.environ.get('SHELL', 'unknown')}",
            f"OS Version: {platform.release()}",
            f"Current date: {date.today().isoformat()}",
        ]

        yield PromptBlock(name=self._id.name, content="\n".join(lines))


class GitPromptProvider(PromptProvider):
    """Текущее состояние git."""

    def __init__(self) -> None:
        self._id = PromptId("git_status")

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return 80

    def blocks(self, state: PromtState[AgentContext]) -> Iterable[PromptBlock]:
        branch = self._git("branch", "--show-current")
        status = self._git("status", "--short")
        log = self._git("log", "--oneline", "-5")
        content = (
            f"Current branch: {branch}\n\nStatus:\n{status}\n\nRecent commits:\n{log}"
        )

        yield PromptBlock(name=self._id.name, content=content)

    @staticmethod
    def _git(*args: str) -> str:
        try:
            result = subprocess.run(
                args=args,
                executable="git",
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            return result.stdout.strip()
        except Exception:
            return "(unavailable)"


class UserQueryProvider(PromptProvider):
    """Запрос пользователя из AgentContext."""

    def __init__(self) -> None:
        self._id = PromptId("user_query")

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return 50

    def blocks(self, state: PromtState[AgentContext]) -> Iterable[PromptBlock]:
        yield PromptBlock(name=self._id.name, content=state.ctx.request.query)


class IDESelectionProvider(PromptProvider):
    """Контекст выделенных строк из IDE."""

    def __init__(self, file_path: str, selection: str) -> None:
        self._id = PromptId("ide_selection")
        self._file_path = file_path
        self._selection = selection

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return 30

    def blocks(self, state: PromtState[AgentContext]) -> Iterable[PromptBlock]:
        content = f"Selected code from {self._file_path}:\n```\n{self._selection}\n```"

        yield PromptBlock(name=self._id.name, content=content)


class TemplateProvider(PromptProvider):
    """Оборачивает запрос в шаблон с инструкциями."""

    def __init__(self, prompt_id: PromptId, priority: int, template: str) -> None:
        self._id = prompt_id
        self._priority = priority
        self._template = template

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def blocks(self, state: PromtState[AgentContext]) -> Iterable[PromptBlock]:
        content = self._template.format(query=state.ctx.request.query)

        yield PromptBlock(name=self._id.name, content=content)
