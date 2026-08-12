"""Песочница монтирует на запись только то, что задано в rw_binds."""

from __future__ import annotations

import os

import pytest
from langchain_core.messages import HumanMessage

from boba.chainlit.domain.keys import ObjectKey
from boba.chainlit.infra.providers import build_llm_view
from boba.sandbox import WORKSPACE_MOUNT
from boba.sandbox.argv import ChannelArgv, WrapArgsCodec
from boba.sandbox.profile import BindSpec, SandboxProfile


def _bin_dirs() -> list[str]:
    """В тестах каталоги берутся из PATH; в проде их задаёт конфиг."""
    dirs: list[str] = []

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.startswith("/"):
            continue

        dirs.append(entry)

    return dirs


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


WRAP_ARGS_FD = 3
REDIRECT_PREFIX = "exec >&4 2>&5"


def _options(profile: SandboxProfile) -> tuple[str, ...]:
    """Опции профиля так, как их прочтёт bwrap из канала wrap_args."""
    built = ChannelArgv.build(
        profile,
        "true",
        env={},
        wrap_args_fd=WRAP_ARGS_FD,
        redirect_prefix=REDIRECT_PREFIX,
    )

    return WrapArgsCodec.decode(built.wrap_args)


def _rw_mounts(options: tuple[str, ...]) -> list[str]:
    mounts: list[str] = []
    for index, option in enumerate(options):
        if option in ("--bind", "--bind-try"):
            mounts.append(options[index + 1])

    return mounts


_PROFILE_BASE: dict[str, object] = {
    "rootfs": "",
    "ro_binds": (),
    "rw_binds": (),
    "rw_images": (),
    "image_template": "",
    "launcher": {
        "mount_wait_sec": 10.0,
        "mount_poll_sec": 0.05,
        "shutdown_wait_sec": 5.0,
        "lock_wait_sec": 10.0,
        "copy_chunk_bytes": 1 << 20,
    },
    "binaries": {"dirs": _bin_dirs()},
    "tmpfs": (),
    "network": False,
    "env_set": {},
    "timeout_sec": 30,
    "max_memory_bytes": 512 * 1024 * 1024,
    "max_cpu_sec": 30,
    "max_file_size_bytes": 64 * 1024 * 1024,
    "max_open_files": 256,
    "max_processes": 256,
    "max_output_bytes": 4 * 1024 * 1024,
    "cgroup_base": "",
    "oom_score_adj": 0,
    "cwd": "",
}


def _profile(**kw: object) -> SandboxProfile:
    """Все поля профиля обязательны; база даёт валидный минимум для тестов."""
    return SandboxProfile.model_validate({**_PROFILE_BASE, **kw})


class TestOnlyConfiguredMounts:
    WS = "/srv/workspace/7/thread-1"

    def test_no_rw_mounts_without_rw_binds(self) -> None:
        assert _rw_mounts(_options(_profile(ro_binds=()))) == []

    def test_single_rw_mount_from_config(self) -> None:
        profile = _profile(ro_binds=(), rw_binds=(self.WS,))
        assert _rw_mounts(_options(profile)) == [self.WS]

    def test_project_root_is_not_mounted(self) -> None:
        profile = _profile(ro_binds=(), rw_binds=(self.WS,))
        assert "/app/docker/compose/boba" not in _rw_mounts(_options(profile))

    def test_rootfs_stays_read_only(self) -> None:
        options = _options(_profile(rootfs="/srv/rootfs", ro_binds=()))
        index = options.index("--ro-bind")
        assert options[index + 1 : index + 3] == ("/srv/rootfs", "/")


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
        key = ObjectKey.build("7", "t1", "report.pdf", "el-1").render()
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
        key = ObjectKey.build("4", "t-1", "report.pdf", "el-1")
        assert key.render() == "4/t-1/upload/report.pdf"
        assert f"{WORKSPACE_MOUNT}/{key.in_thread()}" == (
            "/workspace/t-1/upload/report.pdf"
        )

    def test_unnamed_element_falls_back_to_id(self) -> None:
        key = ObjectKey.build("4", "t-1", "", "el-1")
        assert key.render() == "4/t-1/upload/el-1"

    def test_directories_in_name_are_stripped(self) -> None:
        key = ObjectKey.build("4", "t-1", "../../etc/passwd", "el-1")
        assert key.render() == "4/t-1/upload/passwd"

    def test_parse_is_inverse_of_render(self) -> None:
        key = ObjectKey.build("4", "t-1", "report.pdf", "el-1")
        assert ObjectKey.parse(key.render()) == key

    def test_parse_rejects_traversal_and_alien_layout(self) -> None:
        for raw in ("4/t-1/upload/..", "4/t-1/other/report.pdf", "4/t-1/report.pdf"):
            with pytest.raises(ValueError, match="invalid object_key"):
                ObjectKey.parse(raw)
