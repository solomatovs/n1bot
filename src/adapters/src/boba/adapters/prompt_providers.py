"""Конкретные реализации PromptProvider."""

from __future__ import annotations

import os
import platform
import subprocess
from datetime import date
from pathlib import Path

from boba.domain.core.promt import (
    PromptBlock,
    PromptId,
    PromptProvider,
)


class StaticPromptProvider(PromptProvider):
    """Фиксированный текст, зашитый в код."""

    def __init__(self, id: PromptId, priority: int, content: str) -> None:
        self._id = id
        self._priority = priority
        self._content = content

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def build(self) -> PromptBlock:
        return PromptBlock(name=self._id.name, content=self._content)


class FilePromptProvider(PromptProvider):
    """Читает блок из файла на диске."""

    def __init__(
        self,
        id: PromptId,
        priority: int,
        path: Path,
        default_prompt: str = "",
    ) -> None:
        self._id = id
        self._priority = priority
        self._path = path
        self._default_prompt = default_prompt

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def build(self) -> PromptBlock:
        if self._path.exists():
            content = self._path.read_text(encoding="utf-8")
        else:
            content = self._default_prompt
        return PromptBlock(name=self._id.name, content=content)


class EnvironmentPromptProvider(PromptProvider):
    """Информация о среде выполнения."""

    def __init__(self) -> None:
        self._id = PromptId("environment")

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return 60

    def build(self) -> PromptBlock:
        lines = [
            f"Platform: {platform.system()}",
            f"Shell: {os.environ.get('SHELL', 'unknown')}",
            f"OS Version: {platform.release()}",
            f"Current date: {date.today().isoformat()}",
        ]
        return PromptBlock(name=self._id.name, content="\n".join(lines))


class GitPromptProvider(PromptProvider):
    """Текущее состояние git."""

    def __init__(self) -> None:
        self._id = PromptId("git_status")

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return 80

    def build(self) -> PromptBlock:
        branch = self._run("git branch --show-current")
        status = self._run("git status --short")
        log = self._run("git log --oneline -5")
        content = (
            f"Current branch: {branch}\n\n"
            f"Status:\n{status}\n\n"
            f"Recent commits:\n{log}"
        )
        return PromptBlock(name=self._id.name, content=content)

    @staticmethod
    def _run(cmd: str) -> str:
        try:
            result = subprocess.run(
                cmd.split(),
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip()
        except Exception:
            return "(unavailable)"


class UserQueryProvider(PromptProvider):
    """Сырой запрос пользователя. Всегда присутствует."""

    def __init__(self, query: str) -> None:
        self._id = PromptId("user_query")
        self._query = query

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return 50

    def build(self) -> PromptBlock:
        return PromptBlock(name=self._id.name, content=self._query)


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

    def build(self) -> PromptBlock:
        content = (
            f"Selected code from {self._file_path}:\n" f"```\n{self._selection}\n```"
        )
        return PromptBlock(name=self._id.name, content=content)


class TemplateProvider(PromptProvider):
    """Оборачивает запрос в шаблон с инструкциями."""

    def __init__(self, id: PromptId, priority: int, template: str, query: str) -> None:
        self._id = id
        self._priority = priority
        self._template = template
        self._query = query

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def build(self) -> PromptBlock:
        content = self._template.format(query=self._query)
        return PromptBlock(name=self._id.name, content=content)
