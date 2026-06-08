"""System-prompt: дефолт из директории + per-thread провайдер.

DefaultSystemPromptSource — единый источник истины для «дефолта»:
склейка файлов system_prompt_dir тем же способом, что DirectoryPromptProvider.
Используется UI (initial для шестерёнки), seed для новых ThreadMeta и fallback
для ThreadSystemPromptProvider.

ThreadSystemPromptProvider — PromptProvider, который на каждом turn'е
читает свежий ThreadMeta.system_prompt через sync-метод репозитория.
Это позволяет менять промпт через шестерёнку без пересборки ChatSession.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from boba.agent.prompt import PromptBlock, PromptId, PromptProvider, PromptState
from boba.chainlit.models import ThreadId
from boba.chainlit.storage import ThreadRepository

__all__ = ["DefaultSystemPromptSource", "ThreadSystemPromptProvider"]

logger = logging.getLogger(__name__)


class DefaultSystemPromptSource:
    """
    Склейка всех файлов system_prompt_dir в одну строку
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

        paths = filter(lambda x: x.is_file(), self._root.iterdir())
        paths = filter(lambda x: x.suffix in self._extensions, paths)
        paths = sorted(paths)
        blocks = (x.read_text(encoding="utf-8").rstrip("\n") for x in paths)

        return "\n\n".join(blocks)


class ThreadSystemPromptProvider(PromptProvider):
    """
    Provider, читающий system-prompt из ThreadMeta по thread_id
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
        logger.info(
            "ThreadSystemPromptProvider thread=%s meta=%s source=%s head=%r",
            self._thread_id,
            meta is not None,
            "meta" if (meta is not None and meta.system_prompt) else "fallback",
            content[:80],
        )
        yield PromptBlock(name=self._id, content=content)
