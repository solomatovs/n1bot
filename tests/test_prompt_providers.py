"""Тесты WorkspaceSystemPromptProvider."""

from __future__ import annotations

from pathlib import Path

import pytest

from boba.adapters.fs_workspace import FsWorkspaceService
from boba.adapters.prompt_providers import WorkspaceSystemPromptProvider
from boba.domain.core.promt import PromptId
from boba.domain.core.workspace import WorkspaceId


@pytest.fixture
def ws(tmp_path: Path) -> FsWorkspaceService:
    return FsWorkspaceService(WorkspaceId.new(), tmp_path)


class TestWorkspaceSystemPromptProvider:
    location = "prompts/system"

    def test_missing_directory_yields_empty_iterator(
        self, ws: FsWorkspaceService
    ) -> None:
        provider = WorkspaceSystemPromptProvider(
            PromptId("system"),
            priority=10,
            workspace=ws,
            directory=self.location,
        )
        assert list(provider.blocks()) == []

    def test_reads_files_in_lexicographic_order(self, ws: FsWorkspaceService) -> None:
        ws.mkdir("prompts/system")
        with ws.write_text("prompts/system/02-rules.md") as f:
            f.write("Rules block")
        with ws.write_text("prompts/system/01-intro.md") as f:
            f.write("Intro block")

        provider = WorkspaceSystemPromptProvider(
            PromptId("system"),
            priority=10,
            workspace=ws,
            directory=self.location,
        )
        blocks = list(provider.blocks())

        assert [b.content for b in blocks] == ["Intro block", "Rules block"]
        assert [b.name for b in blocks] == [
            "prompts/system/01-intro.md",
            "prompts/system/02-rules.md",
        ]

    def test_empty_files_skipped(self, ws: FsWorkspaceService) -> None:
        ws.mkdir("prompts/system")
        with ws.write_text("prompts/system/a.md") as f:
            f.write("A")
        with ws.write_text("prompts/system/b.md") as f:
            f.write("   \n\n   ")
        with ws.write_text("prompts/system/c.md") as f:
            f.write("C")

        provider = WorkspaceSystemPromptProvider(
            PromptId("system"),
            priority=10,
            workspace=ws,
            directory=self.location,
        )
        blocks = list(provider.blocks())

        assert [b.content for b in blocks] == ["A", "C"]

    def test_custom_directory(self, ws: FsWorkspaceService) -> None:
        ws.mkdir("custom")
        with ws.write_text("custom/one.txt") as f:
            f.write("hello")

        provider = WorkspaceSystemPromptProvider(
            PromptId("system"),
            priority=10,
            workspace=ws,
            directory="custom",
        )
        blocks = list(provider.blocks())

        assert [b.content for b in blocks] == ["hello"]

    def test_id_and_priority(self, ws: FsWorkspaceService) -> None:
        provider = WorkspaceSystemPromptProvider(
            PromptId("sys"),
            priority=42,
            workspace=ws,
            directory=self.location,
        )
        assert provider.id() == PromptId("sys")
        assert provider.priority() == 42
