"""Песочница монтирует на запись только то, что задано в rw_binds."""

from __future__ import annotations

import os

import pytest
from langchain_core.messages import HumanMessage
from zygote_stand import ROOTFS_IMAGE, ProfileFields

_ROOT = "/tmp/boba-rootfs"  # noqa: S108
"""Точка, куда цепочка лаунчера смонтировала корень."""

from boba.chainlit.domain.keys import ObjectKey, WorkspaceMount
from boba.chainlit.infra.providers import build_llm_view
from boba.sandbox.argv import build_zygote_argv
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


def _rw_mounts(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a in ("--bind", "--bind-try")]


_PROFILE_BASE: dict[str, object] = {
    "host": {
        "mounting": {
            "mount_wait_sec": 10.0,
            "mount_poll_sec": 0.05,
            "shutdown_wait_sec": 5.0,
            "lock_wait_sec": 10.0,
            "copy_chunk_bytes": 1 << 20,
        },
        "binaries": {"dirs": _bin_dirs()},
        "stderr_tail_bytes": 4096,
        "channel_limit_bytes": 67108864,
        "fail_tail_chars": 2000,
        "kill_grace_sec": 5,
        "cgroup_base": "",
    },
    "rootfs": str(ROOTFS_IMAGE),
    "mounts": {
        "tmp": "64M",
        "ro": (),
        "rw": (),
    },
    "isolation": {
        "reap_poll_sec": 0.05,
        "network": False,
        "env": {},
    },
    "limits": {
        "timeout_sec": 30,
        "process_memory_bytes": 512 * 1024 * 1024,
        "process_cpu_sec": 30,
        "process_file_bytes": 64 * 1024 * 1024,
        "process_open_files": 256,
        "process_oom_score_adj": 0,
    },
    "run": {
        "shell": "/bin/bash",
        "cwd": "",
    },
}


def _profile(**kw: object) -> SandboxProfile:
    """Все поля профиля обязательны; база даёт валидный минимум для тестов."""
    return SandboxProfile.model_validate(ProfileFields.merged(_PROFILE_BASE, kw))


MOUNT = "/workspace"
"""Точка рабочего каталога; в приложении её задаёт профиль песочницы."""


@pytest.fixture(autouse=True)
def workspace_mount() -> None:
    """В приложении точку ставит загрузчик инструментов из профиля."""
    WorkspaceMount.configure(MOUNT)


class TestOnlyConfiguredMounts:
    WS = "/srv/workspace/7/thread-1"

    def test_no_rw_mounts_without_rw_binds(self) -> None:
        argv = build_zygote_argv(_profile(ro=()), ["true"], env={}, root=_ROOT)
        if _rw_mounts(argv) != []:
            raise AssertionError("_rw_mounts(argv) == []")

    def test_single_rw_mount_from_config(self) -> None:
        profile = _profile(ro=(), rw=(self.WS,))
        if _rw_mounts(build_zygote_argv(profile, ["true"], env={}, root=_ROOT)) != [self.WS]:
            raise AssertionError("_rw_mounts(...) == [self.WS]")

    def test_project_root_is_not_mounted(self) -> None:
        profile = _profile(ro=(), rw=(self.WS,))
        argv = build_zygote_argv(profile, ["true"], env={}, root=_ROOT)
        if "/app/docker/compose/boba" in _rw_mounts(argv):
            raise AssertionError('"/app/docker/compose/boba" not in _rw_mounts(argv)')

    def test_rootfs_stays_read_only(self) -> None:
        argv = build_zygote_argv(_profile(ro=()), ["true"], env={}, root=_ROOT)
        i = argv.index("--ro-bind")
        if argv[i + 1 : i + 3] != [_ROOT, "/"]:
            raise AssertionError("argv[i + 1 : i + 3] == [_ROOT, /]")


class TestBindSpec:
    def test_parse_without_target_mounts_same_path(self) -> None:
        spec = BindSpec.parse("/srv/data")
        if (spec.host, spec.target) != ("/srv/data", "/srv/data"):
            raise AssertionError('(spec.host, spec.target) == ("/srv/data", "/srv/dat…')

    def test_parse_with_target(self) -> None:
        spec = BindSpec.parse("/srv/ws/{user_id}/{thread_id}:/workspace")
        if spec.host != "/srv/ws/{user_id}/{thread_id}":
            raise AssertionError('spec.host == "/srv/ws/{user_id}/{thread_id}"')
        if spec.target != "/workspace":
            raise AssertionError('spec.target == "/workspace"')

    def test_unknown_variable_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown variables"):
            BindSpec.parse("/srv/{whoami}")

    def test_relative_target_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be absolute"):
            BindSpec.parse("/srv/data:relative/path")

    def test_render_substitutes_both_sides(self) -> None:
        spec = BindSpec.parse("/srv/ws/{user_id}/{thread_id}:/workspace")
        rendered = spec.render({"user_id": "7", "thread_id": "t1"})
        if rendered.host != "/srv/ws/7/t1":
            raise AssertionError('rendered.host == "/srv/ws/7/t1"')
        if rendered.target != "/workspace":
            raise AssertionError('rendered.target == "/workspace"')

    def test_missing_variable_fails_loudly(self) -> None:
        spec = BindSpec.parse("/srv/ws/{thread_id}")
        with pytest.raises(RuntimeError, match="no chainlit session"):
            spec.render({})


class TestProfileRender:
    TEMPLATE = "/srv/ws/{user_id}/{thread_id}:/workspace"

    def _rendered_host(self, user: str, thread: str) -> str:
        profile = _profile(rw=(self.TEMPLATE,), cwd="/workspace")
        rendered = profile.render({"user_id": user, "thread_id": thread})

        return rendered.mounts.rw[0].host

    def test_threads_of_one_user_are_isolated(self) -> None:
        if self._rendered_host("7", "t1") == self._rendered_host("7", "t2"):
            raise AssertionError('self._rendered_host("7", "t1") != self._rendered_ho…')

    def test_users_are_isolated(self) -> None:
        if self._rendered_host("7", "t1") == self._rendered_host("8", "t1"):
            raise AssertionError('self._rendered_host("7", "t1") != self._rendered_ho…')

    def test_cwd_is_rendered(self) -> None:
        profile = _profile(cwd="/srv/ws/{user_id}/{thread_id}")
        rendered = profile.render({"user_id": "7", "thread_id": "t1"})
        if rendered.run.cwd != "/srv/ws/7/t1":
            raise AssertionError('rendered.run.cwd == "/srv/ws/7/t1"')

    def test_static_profile_needs_no_session(self) -> None:
        profile = _profile(rw=("/srv/shared",), cwd="/srv/shared")
        rendered = profile.render({})
        if rendered.mounts.rw[0].host != "/srv/shared":
            raise AssertionError('rendered.mounts.rw[0].host == "/srv/shared"')

    def test_uploads_live_inside_the_chat_folder(self) -> None:
        key = ObjectKey.build("7", "t1", "report.pdf", "el-1").render()
        host = self._rendered_host("7", "t1")
        if not (f"/srv/ws/{key}".startswith(f"{host}/upload/")):
            raise AssertionError('f"/srv/ws/{key}".startswith(f"{host}/upload/")')


class TestAttachmentPaths:
    """Пути вложений уходят в LLM, но не в ленту."""

    @staticmethod
    def _message(*attachments: dict[str, str]) -> HumanMessage:
        extra = {"attachments": list(attachments)} if attachments else {}
        return HumanMessage(content="разбери файл", id="u1", additional_kwargs=extra)

    def test_llm_sees_sandbox_path(self) -> None:
        msg = self._message({"name": "data.csv", "path": "/workspace/upload/el-1"})
        if "/workspace/upload/el-1" not in build_llm_view([msg])[0].content:
            raise AssertionError('"/workspace/upload/el-1" in build_llm_view([msg])[0…')

    def test_llm_sees_file_name(self) -> None:
        msg = self._message({"name": "data.csv", "path": "/workspace/upload/el-1"})
        if "data.csv" not in build_llm_view([msg])[0].content:
            raise AssertionError('"data.csv" in build_llm_view([msg])[0].content')

    def test_original_message_is_not_touched(self) -> None:
        msg = self._message({"name": "data.csv", "path": "/workspace/upload/el-1"})
        build_llm_view([msg])
        if msg.content != "разбери файл":
            raise AssertionError('msg.content == "разбери файл"')

    def test_message_without_attachments_unchanged(self) -> None:
        if build_llm_view([self._message()])[0].content != "разбери файл":
            raise AssertionError('build_llm_view([self._message()])[0].content == "ра…')

    def test_several_attachments_listed(self) -> None:
        msg = self._message(
            {"name": "a.csv", "path": "/workspace/upload/1"},
            {"name": "b.csv", "path": "/workspace/upload/2"},
        )
        content = build_llm_view([msg])[0].content
        if "a.csv" not in content:
            raise AssertionError('"a.csv" in content')
        if "b.csv" not in content:
            raise AssertionError('"b.csv" in content')

    def test_path_matches_where_storage_puts_the_file(self) -> None:
        key = ObjectKey.build("4", "t-1", "report.pdf", "el-1")
        if key.render() != "4/t-1/upload/report.pdf":
            raise AssertionError('key.render() == "4/t-1/upload/report.pdf"')
        if f"{MOUNT}/{key.in_thread()}" != "/workspace/t-1/upload/report.pdf":
            raise AssertionError('f"{MOUNT}/{key.in_thread()}" == ( "/works…')

    def test_unnamed_element_falls_back_to_id(self) -> None:
        key = ObjectKey.build("4", "t-1", "", "el-1")
        if key.render() != "4/t-1/upload/el-1":
            raise AssertionError('key.render() == "4/t-1/upload/el-1"')

    def test_directories_in_name_are_stripped(self) -> None:
        key = ObjectKey.build("4", "t-1", "../../etc/passwd", "el-1")
        if key.render() != "4/t-1/upload/passwd":
            raise AssertionError('key.render() == "4/t-1/upload/passwd"')

    def test_parse_is_inverse_of_render(self) -> None:
        key = ObjectKey.build("4", "t-1", "report.pdf", "el-1")
        if ObjectKey.parse(key.render()) != key:
            raise AssertionError("ObjectKey.parse(key.render()) == key")

    def test_parse_rejects_traversal_and_alien_layout(self) -> None:
        for raw in ("4/t-1/upload/..", "4/t-1/other/report.pdf", "4/t-1/report.pdf"):
            with pytest.raises(ValueError, match="invalid object_key"):
                ObjectKey.parse(raw)
