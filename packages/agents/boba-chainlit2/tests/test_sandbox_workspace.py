"""Песочница монтирует на запись только то, что задано в rw_binds."""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from boba.chainlit2.agent.tools.sandbox import WORKSPACE_MOUNT
from boba.chainlit2.agent.tools.sandbox.argv import build_bwrap_argv
from boba.chainlit2.agent.tools.sandbox.profile import BindSpec, SandboxProfile
from boba.chainlit2.chat.data.models import Element
from boba.chainlit2.infra.providers import build_llm_view


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _rw_mounts(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a in ("--bind", "--bind-try")]


_PROFILE_BASE: dict[str, object] = {
    "rootfs": "",
    "ro_binds": (),
    "rw_binds": (),
    "rw_images": (),
    "image_template": "",
    "launcher": {},
    "tmpfs": (),
    "network": False,
    "env_set": {},
    "timeout_sec": 30,
    "max_memory_bytes": 512 * 1024 * 1024,
    "max_cpu_sec": 30,
    "max_file_size_bytes": 64 * 1024 * 1024,
    "max_open_files": 256,
    "max_processes": 256,
    "max_output_bytes": 256 * 1024,
    "cwd": "",
}


def _profile(**kw: object) -> SandboxProfile:
    """Все поля профиля обязательны; база даёт валидный минимум для тестов."""
    return SandboxProfile.model_validate({**_PROFILE_BASE, **kw})


class TestOnlyConfiguredMounts:
    WS = "/srv/workspace/7/thread-1"

    def test_no_rw_mounts_without_rw_binds(self) -> None:
        argv = build_bwrap_argv(_profile(ro_binds=()), "true", env={})
        assert _rw_mounts(argv) == []

    def test_single_rw_mount_from_config(self) -> None:
        profile = _profile(ro_binds=(), rw_binds=(self.WS,))
        assert _rw_mounts(build_bwrap_argv(profile, "true", env={})) == [self.WS]

    def test_project_root_is_not_mounted(self) -> None:
        profile = _profile(ro_binds=(), rw_binds=(self.WS,))
        argv = build_bwrap_argv(profile, "true", env={})
        assert "/app/docker/compose/boba" not in _rw_mounts(argv)

    def test_rootfs_stays_read_only(self) -> None:
        argv = build_bwrap_argv(
            _profile(rootfs="/srv/rootfs", ro_binds=()), "true", env={},
        )
        i = argv.index("--ro-bind")
        assert argv[i + 1 : i + 3] == ["/srv/rootfs", "/"]


class TestBindSpec:
    def test_parse_without_target_mounts_same_path(self) -> None:
        spec = BindSpec.parse("/srv/data")
        assert (spec.host, spec.target) == ("/srv/data", "/srv/data")

    def test_parse_with_target(self) -> None:
        spec = BindSpec.parse("/srv/ws/{user_id}/{thread_id}:/workspace")
        assert spec.host == "/srv/ws/{user_id}/{thread_id}"
        assert spec.target == "/workspace"

    def test_unknown_variable_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown variables"):
            BindSpec.parse("/srv/{whoami}")

    def test_relative_target_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be absolute"):
            BindSpec.parse("/srv/data:relative/path")

    def test_render_substitutes_both_sides(self) -> None:
        spec = BindSpec.parse("/srv/ws/{user_id}/{thread_id}:/workspace")
        rendered = spec.render({"user_id": "7", "thread_id": "t1"})
        assert rendered.host == "/srv/ws/7/t1"
        assert rendered.target == "/workspace"

    def test_missing_variable_fails_loudly(self) -> None:
        spec = BindSpec.parse("/srv/ws/{thread_id}")
        with pytest.raises(RuntimeError, match="no chainlit session"):
            spec.render({})


class TestProfileRender:
    TEMPLATE = "/srv/ws/{user_id}/{thread_id}:/workspace"

    def _rendered_host(self, user: str, thread: str) -> str:
        profile = _profile(rw_binds=(self.TEMPLATE,), cwd="/workspace")
        return profile.render({"user_id": user, "thread_id": thread}).rw_binds[0].host

    def test_threads_of_one_user_are_isolated(self) -> None:
        assert self._rendered_host("7", "t1") != self._rendered_host("7", "t2")

    def test_users_are_isolated(self) -> None:
        assert self._rendered_host("7", "t1") != self._rendered_host("8", "t1")

    def test_cwd_is_rendered(self) -> None:
        profile = _profile(cwd="/srv/ws/{user_id}/{thread_id}")
        rendered = profile.render({"user_id": "7", "thread_id": "t1"})
        assert rendered.cwd == "/srv/ws/7/t1"

    def test_static_profile_needs_no_session(self) -> None:
        profile = _profile(rw_binds=("/srv/shared",), cwd="/srv/shared")
        rendered = profile.render({})
        assert rendered.rw_binds[0].host == "/srv/shared"

    def test_uploads_live_inside_the_chat_folder(self) -> None:
        key = Element.object_key("7", "t1", "el-1")
        host = self._rendered_host("7", "t1")
        assert f"/srv/ws/{key}".startswith(f"{host}/upload/")


class TestAttachmentPaths:
    """Пути вложений уходят в LLM, но не в ленту."""

    @staticmethod
    def _message(*attachments: dict[str, str]) -> HumanMessage:
        extra = {"attachments": list(attachments)} if attachments else {}
        return HumanMessage(content="разбери файл", id="u1", additional_kwargs=extra)

    def test_llm_sees_sandbox_path(self) -> None:
        msg = self._message({"name": "data.csv", "path": "/workspace/upload/el-1"})
        assert "/workspace/upload/el-1" in build_llm_view([msg])[0].content

    def test_llm_sees_file_name(self) -> None:
        msg = self._message({"name": "data.csv", "path": "/workspace/upload/el-1"})
        assert "data.csv" in build_llm_view([msg])[0].content

    def test_original_message_is_not_touched(self) -> None:
        msg = self._message({"name": "data.csv", "path": "/workspace/upload/el-1"})
        build_llm_view([msg])
        assert msg.content == "разбери файл"

    def test_message_without_attachments_unchanged(self) -> None:
        assert build_llm_view([self._message()])[0].content == "разбери файл"

    def test_several_attachments_listed(self) -> None:
        msg = self._message(
            {"name": "a.csv", "path": "/workspace/upload/1"},
            {"name": "b.csv", "path": "/workspace/upload/2"},
        )
        content = build_llm_view([msg])[0].content
        assert "a.csv" in content
        assert "b.csv" in content

    def test_path_matches_where_storage_puts_the_file(self) -> None:
        key = Element.object_key("4", "t-1", "el-1")
        assert key == "4/t-1/upload/el-1"
        assert f"{WORKSPACE_MOUNT}/upload/el-1" == "/workspace/upload/el-1"
