"""Песочница монтирует на запись только то, что задано в rw_binds."""

from __future__ import annotations

import os

import pytest

from boba.stand.zygote import ROOTFS_IMAGE, ProfileFields, SandboxCgroup

_ROOT = "/tmp/boba-rootfs"  # noqa: S108
"""Точка, куда цепочка лаунчера смонтировала корень."""

from boba.canvas.keys import ObjectKey, WorkspaceMount
from boba.sandbox.argv import ZygoteArgv
from boba.sandbox.profile import BindSpec, SandboxProfile


def _bin_dirs() -> list[str]:
    """В тестах каталоги берутся из PATH; в проде их задаёт конфиг."""
    dirs: list[str] = []

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.startswith("/"):
            continue

        dirs.append(entry)

    return dirs


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
        "cgroup_base": SandboxCgroup().base,
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
        "group_memory_bytes": 2 * 1024 * 1024 * 1024,
        "process_oom_score_adj": 0,
    },
    "run": {
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
        argv = ZygoteArgv(_profile(ro=())).argv(["true"], env={}, root=_ROOT)
        if _rw_mounts(argv) != []:
            raise AssertionError("_rw_mounts(argv) == []")

    def test_single_rw_mount_from_config(self) -> None:
        profile = _profile(ro=(), rw=(self.WS,))
        if _rw_mounts(ZygoteArgv(profile).argv(["true"], env={}, root=_ROOT)) != [
            self.WS
        ]:
            raise AssertionError("_rw_mounts(...) == [self.WS]")

    def test_project_root_is_not_mounted(self) -> None:
        profile = _profile(ro=(), rw=(self.WS,))
        argv = ZygoteArgv(profile).argv(["true"], env={}, root=_ROOT)
        if "/app/docker/compose/boba" in _rw_mounts(argv):
            raise AssertionError('"/app/docker/compose/boba" not in _rw_mounts(argv)')

    def test_rootfs_stays_read_only(self) -> None:
        argv = ZygoteArgv(_profile(ro=())).argv(["true"], env={}, root=_ROOT)
        i = argv.index("--ro-bind")
        if argv[i + 1 : i + 3] != [_ROOT, "/"]:
            raise AssertionError("argv[i + 1 : i + 3] == [_ROOT, /]")


class TestBindSpec:
    def test_parse_without_target_mounts_same_path(self) -> None:
        spec = BindSpec.parse("/srv/data")
        if (spec.host, spec.target) != ("/srv/data", "/srv/data"):
            raise AssertionError('(spec.host, spec.target) == ("/srv/data", "/srv/dat…')

    def test_parse_with_target(self) -> None:
        spec = BindSpec.parse("/srv/ws/{user_id}/{scope_id}:/workspace")
        if spec.host != "/srv/ws/{user_id}/{scope_id}":
            raise AssertionError('spec.host == "/srv/ws/{user_id}/{scope_id}"')
        if spec.target != "/workspace":
            raise AssertionError('spec.target == "/workspace"')

    def test_unknown_variable_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown variables"):
            BindSpec.parse("/srv/{whoami}")

    def test_relative_target_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be absolute"):
            BindSpec.parse("/srv/data:relative/path")

    def test_render_substitutes_both_sides(self) -> None:
        spec = BindSpec.parse("/srv/ws/{user_id}/{scope_id}:/workspace")
        rendered = spec.render({"user_id": "7", "scope_id": "t1"})
        if rendered.host != "/srv/ws/7/t1":
            raise AssertionError('rendered.host == "/srv/ws/7/t1"')
        if rendered.target != "/workspace":
            raise AssertionError('rendered.target == "/workspace"')

    def test_missing_variable_fails_loudly(self) -> None:
        spec = BindSpec.parse("/srv/ws/{scope_id}")
        with pytest.raises(RuntimeError, match="no user session"):
            spec.render({})

    def test_half_filled_session_names_what_it_has(self) -> None:
        """Сессия есть, но пользователь без id: отказ не врёт про её отсутствие."""
        spec = BindSpec.parse("/srv/ws/{user_id}.ext4")

        with pytest.raises(RuntimeError) as caught:
            spec.render({"scope_id": "t1"})

        message = str(caught.value)
        if "no user session" in message:
            raise AssertionError(f"причина названа неверно: {message}")

        if "known: scope_id" not in message:
            raise AssertionError(f"отказ перечисляет, что есть: {message}")


class TestProfileRender:
    TEMPLATE = "/srv/ws/{user_id}/{scope_id}:/workspace"

    def _rendered_host(self, user: str, thread: str) -> str:
        profile = _profile(rw=(self.TEMPLATE,), cwd="/workspace")
        rendered = profile.render({"user_id": user, "scope_id": thread})

        return rendered.mounts.rw[0].host

    def test_scopes_of_one_user_are_isolated(self) -> None:
        if self._rendered_host("7", "t1") == self._rendered_host("7", "t2"):
            raise AssertionError('self._rendered_host("7", "t1") != self._rendered_ho…')

    def test_users_are_isolated(self) -> None:
        if self._rendered_host("7", "t1") == self._rendered_host("8", "t1"):
            raise AssertionError('self._rendered_host("7", "t1") != self._rendered_ho…')

    def test_cwd_is_rendered(self) -> None:
        profile = _profile(cwd="/srv/ws/{user_id}/{scope_id}")
        rendered = profile.render({"user_id": "7", "scope_id": "t1"})
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


class TestDataImages:
    """Каталог данных едет во вложенный слой строгим ro-биндом, а внешний
    слой показывает его источник."""

    IMAGE = "/srv/models/fastembed"
    TARGET = "/var/cache/fastembed"

    def test_image_is_bound_read_only_to_its_target(self) -> None:
        profile = _profile(images=(f"{self.IMAGE}:{self.TARGET}",))
        argv = ZygoteArgv(profile).argv(["true"], env={}, root=_ROOT)

        pairs: list[list[str]] = []
        for index, arg in enumerate(argv):
            if arg == "--ro-bind":
                pairs.append(argv[index + 1 : index + 3])

        if [self.IMAGE, self.TARGET] not in pairs:
            raise AssertionError(f"no strict read-only bind of the data dir: {pairs}")

    def test_outer_layer_shows_the_data_directory(self) -> None:
        profile = _profile(images=(f"{self.IMAGE}:{self.TARGET}",))
        sources = ZygoteArgv(profile).sources()

        if self.IMAGE not in sources.ro:
            raise AssertionError(f"read-only sources of the outer layer: {sources.ro}")

    def test_per_call_variable_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="mounted once per zygote"):
            _profile(images=("/srv/{user_id}.ext4:/var/cache/x",))
