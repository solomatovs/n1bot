"""Песочница получает на запись только папку текущего чата."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from boba.chainlit2.agent.tools.sandbox.argv import build_bwrap_argv
from boba.chainlit2.agent.tools.sandbox.profile import SandboxProfile
from boba.chainlit2.chat.data.models import Element
from boba.chainlit2.infra.session import current_workspace


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _rw_mounts(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a in ("--bind", "--bind-try")]


class TestSingleWritableMount:
    WS = "/srv/workspace/7/thread-1"

    def _argv(self, profile: SandboxProfile | None = None) -> list[str]:
        return build_bwrap_argv(
            profile or SandboxProfile(ro_binds=()),
            "true",
            workspace_root=self.WS,
            env={},
        )

    def test_only_one_writable_mount(self) -> None:
        assert len(_rw_mounts(self._argv())) == 1

    def test_writable_mount_is_the_chat_folder(self) -> None:
        assert _rw_mounts(self._argv())[0] == self.WS

    def test_project_root_is_not_mounted(self) -> None:
        assert "/app/docker/compose/boba" not in _rw_mounts(self._argv())

    def test_rootfs_stays_read_only(self) -> None:
        argv = self._argv(SandboxProfile(rootfs="/srv/rootfs", ro_binds=()))
        i = argv.index("--ro-bind")
        assert argv[i + 1 : i + 3] == ["/srv/rootfs", "/"]


class TestWorkspacePerChat:
    @staticmethod
    def _workspace(base: Path, user: str, thread: str) -> Path:
        with (
            patch(
                "boba.chainlit2.infra.session.current_user_id", return_value=user
            ),
            patch(
                "boba.chainlit2.infra.session.current_thread_id", return_value=thread
            ),
        ):
            return current_workspace(base)

    def test_path_is_user_then_thread(self, tmp_path: Path) -> None:
        assert self._workspace(tmp_path, "7", "t1") == tmp_path / "7" / "t1"

    def test_directory_is_created(self, tmp_path: Path) -> None:
        assert self._workspace(tmp_path, "7", "t1").is_dir()

    def test_threads_of_one_user_are_isolated(self, tmp_path: Path) -> None:
        first = self._workspace(tmp_path, "7", "t1")
        second = self._workspace(tmp_path, "7", "t2")
        assert first != second

    def test_users_are_isolated(self, tmp_path: Path) -> None:
        first = self._workspace(tmp_path, "7", "t1")
        second = self._workspace(tmp_path, "8", "t1")
        assert first != second

    def test_uploads_live_inside_the_chat_folder(self, tmp_path: Path) -> None:
        chat = self._workspace(tmp_path, "7", "t1")
        key = Element.object_key("7", "t1", "el-1")
        assert (tmp_path / key).parent == chat / "upload"

    def test_without_session_it_fails_loudly(self, tmp_path: Path) -> None:
        with (
            patch("boba.chainlit2.infra.session.current_user_id", return_value=None),
            pytest.raises(RuntimeError, match="нет сессии"),
        ):
            current_workspace(tmp_path)
