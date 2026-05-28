"""System-prompt: дефолт из директории + per-thread провайдер.

`DefaultSystemPromptSource` — единый источник истины для «дефолта»:
склейка файлов system_prompt_dir тем же способом, что `DirectoryPromptProvider`.
Используется UI (initial для шестерёнки), seed для новых ThreadMeta и fallback
для `ThreadSystemPromptProvider`.

`ThreadSystemPromptProvider` — `PromptProvider`, который на каждом turn'е
читает свежий `ThreadMeta.system_prompt` через sync-метод репозитория.
Это позволяет менять промпт через шестерёнку без пересборки ChatSession.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from boba.agent.prompt import PromptBlock, PromptId, PromptProvider, PromptState
from boba.chainlit.agent.models import ThreadId
from boba.chainlit.agent.storage import ThreadRepository

__all__ = ["DefaultSystemPromptSource", "ThreadSystemPromptProvider"]


class DefaultSystemPromptSource:
    """Склейка всех файлов system_prompt_dir в одну строку.

    Читается каждый раз заново — правки на диске видны без рестарта.
    Алгоритм совпадает с `DirectoryPromptProvider`: сортировка по имени,
    фильтр расширений, склейка непустых блоков через двойной перенос.
    """

    def __init__(
        self,
        root: Path,
        *,
        extensions: tuple[str, ...] = (".md", ".txt"),
    ) -> None:
        self._root = root
        self._extensions = extensions

    def read(self) -> str:
        if not self._root.is_dir():
            return ""
        paths = sorted(
            p for p in self._root.iterdir()
            if p.is_file() and p.suffix in self._extensions
        )
        blocks: list[str] = []
        for path in paths:
            text = path.read_text(encoding="utf-8").rstrip("\n")
            if text:
                blocks.append(text)
        return "\n\n".join(blocks)


class ThreadSystemPromptProvider(PromptProvider):
    """Provider, читающий system-prompt из `ThreadMeta` по `thread_id`.

    Если меты ещё нет (тред не зарегистрирован) или `system_prompt` пуст —
    возвращает `fallback` (типично — дефолт из `DefaultSystemPromptSource`).
    """

    def __init__(
        self,
        prompt_id: PromptId,
        priority: int,
        repository: ThreadRepository,
        thread_id: ThreadId,
        fallback: str,
    ) -> None:
        self._id = prompt_id
        self._priority = priority
        self._repository = repository
        self._thread_id = thread_id
        self._fallback = fallback

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def blocks(self, state: PromptState) -> Iterable[PromptBlock]:
        meta = self._repository.get_meta_sync(self._thread_id)
        content = (meta.system_prompt if meta is not None else None) or self._fallback
        yield PromptBlock(name=self._id, content=content)
