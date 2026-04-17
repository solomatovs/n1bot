"""Тесты FsWorkspaceService: оптимизация ensure-on-demand."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from boba.adapters.fs_workspace import FsWorkspaceService
from boba.domain.core.workspace import WorkspaceId


@pytest.fixture
def ws(tmp_path: Path) -> FsWorkspaceService:
    return FsWorkspaceService(WorkspaceId.new(), tmp_path)


class TestLazyParentMkdir:
    def test_append_to_existing_parent_skips_mkdir(
        self, ws: FsWorkspaceService
    ) -> None:
        with patch.object(Path, "mkdir") as mock_mkdir:
            with ws.append_text("file.txt") as f:
                f.write("hi")
            mock_mkdir.assert_not_called()

    def test_write_to_existing_parent_skips_mkdir(
        self, ws: FsWorkspaceService
    ) -> None:
        with patch.object(Path, "mkdir") as mock_mkdir:
            with ws.write_text("file.txt") as f:
                f.write("hello")
            mock_mkdir.assert_not_called()

    def test_missing_parent_created_on_first_write_and_skipped_on_subsequent(
        self, ws: FsWorkspaceService, tmp_path: Path
    ) -> None:
        assert not (tmp_path / "sub").exists()

        with ws.write_text("sub/a.txt") as f:
            f.write("1")
        assert (tmp_path / "sub" / "a.txt").read_text() == "1"

        with patch.object(Path, "mkdir") as mock_mkdir:
            with ws.append_text("sub/a.txt") as f:
                f.write("2")
            with ws.write_text("sub/b.txt") as f:
                f.write("3")
            mock_mkdir.assert_not_called()

        assert (tmp_path / "sub" / "a.txt").read_text() == "12"
        assert (tmp_path / "sub" / "b.txt").read_text() == "3"

    def test_append_to_missing_parent_creates_and_writes(
        self, ws: FsWorkspaceService, tmp_path: Path
    ) -> None:
        with ws.append_text("deep/nested/new.txt") as f:
            f.write("ok")
        assert (tmp_path / "deep" / "nested" / "new.txt").read_text() == "ok"
