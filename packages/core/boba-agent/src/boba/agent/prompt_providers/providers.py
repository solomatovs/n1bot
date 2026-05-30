"""Реализации PromptProvider (system-prompt)."""

from __future__ import annotations

import os
import platform
import subprocess
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime

from boba.agent.prompt import (
    PromptBlock,
    PromptId,
    PromptProvider,
    PromptState,
)
from boba.workspace.contract import WorkspaceShell


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

    def blocks(self, state: PromptState) -> Iterable[PromptBlock]:
        yield PromptBlock(name=self._id, content=self._content)


class FilePromptProvider(PromptProvider):
    """Читает блок промпта из файла workspace; отсутствующий файл → default_prompt."""

    def __init__(
        self,
        prompt_id: PromptId,
        priority: int,
        workspace: WorkspaceShell,
        rel_path: str,
        default_prompt: str = "",
    ) -> None:
        self._id = prompt_id
        self._priority = priority
        self._workspace = workspace
        self._rel_path = rel_path
        self._default_prompt = default_prompt

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def blocks(self, state: PromptState) -> Iterable[PromptBlock]:
        if self._workspace.exists(self._rel_path):
            with self._workspace.read_text(self._rel_path) as f:
                content = f.read()
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
    """Читает файлы верхнего уровня workspace; каждый файл — отдельный `PromptBlock`.

    Корень поиска — корень самого `workspace`. Если нужна другая директория,
    создай отдельный `WorkspaceShell`, указывающий на неё.

    После 1:1-маппинга `PromptBlock` → `SystemMessage` в SystemPromptReducer
    несколько файлов разворачиваются в несколько system-сообщений, что
    1-в-1 ложится на Anthropic multi-block system.

    Порядок блоков — лексикографический по относительному пути; чтобы
    задать порядок, именуй файлы префиксом (`01-persona.md`, `02-rules.md`).

    Фильтр `extensions` оставляет только файлы с этими расширениями.
    """

    def __init__(
        self,
        prompt_id: PromptId,
        priority: int,
        workspace: WorkspaceShell,
        *,
        extensions: tuple[str, ...] = (".md", ".txt"),
    ) -> None:
        self._id = prompt_id
        self._priority = priority
        self._workspace = workspace
        self._extensions = extensions

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def blocks(self, state: PromptState) -> Iterator[PromptBlock]:
        paths = sorted(e.path for e in self._workspace.ls() if e.kind == "file")
        for rel_path in paths:
            if self._extensions and not rel_path.endswith(self._extensions):
                continue
            with self._workspace.read_text(rel_path) as f:
                content = f.read().rstrip("\n")
            if content:
                yield PromptBlock(name=rel_path, content=content)


class CallablePromptProvider(PromptProvider):
    """Computed-at-runtime блок: `fn()` вызывается на каждом turn.

    Покрывает кейсы «текущая дата», «model capabilities», «runtime-вычисленный
    кусок текста». Если значение статичное — используй `StaticPromptProvider`.
    """

    def __init__(
        self,
        prompt_id: PromptId,
        priority: int,
        fn: Callable[[], str],
    ) -> None:
        self._id = prompt_id
        self._priority = priority
        self._fn = fn

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def blocks(self, state: PromptState) -> Iterable[PromptBlock]:
        yield PromptBlock(name=self._id, content=self._fn())


class WrappingPromptProvider(PromptProvider):
    """Decorator: оборачивает каждый блок inner-провайдера в prefix/suffix.

    Удобно для XML-обёрток вокруг **динамического** содержимого:

        WrappingPromptProvider(
            PromptId("role"), priority=10,
            inner=FilePromptProvider(..., rel_path="role.md"),
            prefix="<your_role>\\n", suffix="\\n</your_role>",
        )

    Для статичного текста проще писать теги прямо в `StaticPromptProvider`.
    `id()` и `priority()` берутся у обёртки, а не у inner — это позволяет
    регистрировать оба независимо при необходимости.
    """

    def __init__(
        self,
        prompt_id: PromptId,
        priority: int,
        inner: PromptProvider,
        *,
        prefix: str = "",
        suffix: str = "",
    ) -> None:
        self._id = prompt_id
        self._priority = priority
        self._inner = inner
        self._prefix = prefix
        self._suffix = suffix

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def blocks(self, state: PromptState) -> Iterable[PromptBlock]:
        for block in self._inner.blocks(state):
            yield PromptBlock(
                name=block.name,
                content=f"{self._prefix}{block.content}{self._suffix}",
            )
