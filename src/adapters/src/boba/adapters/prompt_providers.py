"""Конкретные реализации PromptProvider."""

from __future__ import annotations

import os
import platform
import subprocess
from datetime import date
from pathlib import Path

from boba.domain.agent.models import AgentContext
from boba.domain.core.promt import (
    PromptBlock,
    PromptId,
    PromptProvider,
)


class StaticPromptProvider(PromptProvider[None]):
    """Фиксированный текст, зашитый в код."""

    def __init__(self, prompt_id: PromptId, priority: int, content: str) -> None:
        self._id = prompt_id
        self._priority = priority
        self._content = content

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def build(self, ctx: None) -> PromptBlock:
        return PromptBlock(name=self._id.name, content=self._content)


class FilePromptProvider(PromptProvider[None]):
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

    def build(self, ctx: None) -> PromptBlock:
        if self._path.exists():
            content = self._path.read_text(encoding="utf-8")
        else:
            content = self._default_prompt
        return PromptBlock(name=self._id.name, content=content)


class EnvironmentPromptProvider(PromptProvider[None]):
    """Информация о среде выполнения."""

    def __init__(self) -> None:
        self._id = PromptId("environment")

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return 60

    def build(self, ctx: None) -> PromptBlock:
        lines = [
            f"Platform: {platform.system()}",
            f"Shell: {os.environ.get('SHELL', 'unknown')}",
            f"OS Version: {platform.release()}",
            f"Current date: {date.today().isoformat()}",
        ]
        return PromptBlock(name=self._id.name, content="\n".join(lines))


class GitPromptProvider(PromptProvider[None]):
    """Текущее состояние git."""

    def __init__(self) -> None:
        self._id = PromptId("git_status")

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return 80

    def build(self, ctx: None) -> PromptBlock:
        branch = self._git("branch", "--show-current")
        status = self._git("status", "--short")
        log = self._git("log", "--oneline", "-5")
        content = (
            f"Current branch: {branch}\n\n"
            f"Status:\n{status}\n\n"
            f"Recent commits:\n{log}"
        )
        return PromptBlock(name=self._id.name, content=content)

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


class UserQueryProvider(PromptProvider[AgentContext]):
    """Запрос пользователя из AgentContext."""

    def __init__(self) -> None:
        self._id = PromptId("user_query")

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return 50

    def build(self, ctx: AgentContext) -> PromptBlock:
        return PromptBlock(name=self._id.name, content=ctx.request.query)


class IDESelectionProvider(PromptProvider[AgentContext]):
    """Контекст выделенных строк из IDE."""

    def __init__(self, file_path: str, selection: str) -> None:
        self._id = PromptId("ide_selection")
        self._file_path = file_path
        self._selection = selection

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return 30

    def build(self, ctx: AgentContext) -> PromptBlock:
        content = (
            f"Selected code from {self._file_path}:\n" f"```\n{self._selection}\n```"
        )
        return PromptBlock(name=self._id.name, content=content)


class TemplateProvider(PromptProvider[AgentContext]):
    """Оборачивает запрос в шаблон с инструкциями."""

    def __init__(self, prompt_id: PromptId, priority: int, template: str) -> None:
        self._id = prompt_id
        self._priority = priority
        self._template = template

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def build(self, ctx: AgentContext) -> PromptBlock:
        content = self._template.format(query=ctx.request.query)
        return PromptBlock(name=self._id.name, content=content)
