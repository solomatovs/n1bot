"""File-based loader статических system-prompt блоков."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from boba_next.agent.prompt import PromptId, PromptProvider
from boba_next.workspace import PromptWorkspaceShell, WorkspaceError

from boba.adapter.prompt_providers.providers import StaticPromptProvider

logger = logging.getLogger(__name__)


class PromptLoadError(Exception):
    """Ошибка discovery: чтение файла провалилось."""

    def __init__(self, rel_path: str, message: str) -> None:
        super().__init__(f"prompt {rel_path!r}: {message}")
        self.rel_path = rel_path


_PRIORITY_PREFIX_RE = re.compile(r"^(\d+)-")
_DEFAULT_PRIORITY = 100
_TEXT_EXTENSIONS = (".md", ".txt")


class PromptLoader:
    """Discovery *.md/*.txt из PromptWorkspaceShell в Sequence[PromptProvider]."""

    def __init__(self, workspace: PromptWorkspaceShell) -> None:
        self._workspace = workspace
        self._providers: list[PromptProvider] = []
        self._discover()

    def prompt_providers(self) -> Sequence[PromptProvider]:
        """Закэшированный список StaticPromptProvider."""
        return tuple(self._providers)

    def _discover(self) -> None:
        for rel_path in self._workspace.tree():
            if any(seg.startswith("_") for seg in rel_path.split("/")):
                continue
            if not rel_path.endswith(_TEXT_EXTENSIONS):
                continue
            try:
                self._load(rel_path)
            except PromptLoadError as e:
                logger.warning("%s; skipped", e)

    def _load(self, rel_path: str) -> None:
        try:
            with self._workspace.read_text(rel_path) as f:
                content = f.read()
        except WorkspaceError as e:
            raise PromptLoadError(rel_path, f"read failed: {e}") from e
        if not content.strip():
            logger.info("prompt %r: empty content; skipped", rel_path)
            return
        prompt_id = PromptId(rel_path.removesuffix(".md").removesuffix(".txt"))
        self._providers.append(
            StaticPromptProvider(
                prompt_id=prompt_id,
                priority=self._priority_for(rel_path),
                content=content.rstrip("\n"),
            )
        )

    @staticmethod
    def _priority_for(rel_path: str) -> int:
        name = rel_path.rsplit("/", 1)[-1]
        match = _PRIORITY_PREFIX_RE.match(name)
        if match:
            return int(match.group(1))
        return _DEFAULT_PRIORITY
