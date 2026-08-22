"""Компонент sandbox: профиль запуска инструмента и независимость от tools."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

import pytest
from omegaconf import DictConfig, OmegaConf
from omegaconf.errors import InterpolationKeyError
from zygote_stand import ProfileFields

from boba.sandbox import SandboxConfig, SandboxProfile, SandboxToolConfig
from boba.settings import bind


def _bin_dirs() -> list[str]:
    """В тестах каталоги берутся из PATH; в проде их задаёт конфиг."""
    dirs: list[str] = []

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.startswith("/"):
            continue

        dirs.append(entry)

    return dirs


_PROFILE_BASE: dict[str, Any] = {
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
        "fail_tail_chars": 2000,
        "kill_grace_sec": 5,
        "cgroup_base": "",
    },
    "rootfs": {
        "dir": "",
    },
    "mounts": {
        "setup_ro": (),
        "setup_rw": (),
        "ro": (),
        "rw": (),
        "images": (),
        "image_template": "",
        "tmpfs": (),
        "proc": "/proc",
        "dev": "/dev",
        "call_tmpfs": "/tmp",  # noqa: S108
    },
    "isolation": {
        "reap_poll_sec": 0.05,
        "network": False,
        "env": {},
        "max_processes": 256,
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


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _profile(**kw: Any) -> SandboxProfile:
    return SandboxProfile.model_validate(ProfileFields.merged(_PROFILE_BASE, kw))


def _tool_config(**profile_kw: Any) -> SandboxToolConfig:
    profile = _profile(cwd="/workspace", **profile_kw)
    return SandboxToolConfig.model_validate({"profile": profile})


def _raw_profile(**flat: Any) -> dict[str, Any]:
    """Сырой словарь профиля: им пользуются проверки наследования."""
    return ProfileFields.merged(_PROFILE_BASE, flat)


class TestProfileRegistry:
    """Секция [sandbox] валидируется на старте целиком, а не при первом вызове."""

    def test_profiles_are_parsed(self) -> None:
        cfg = SandboxConfig.model_validate(
            {
                "profiles": {
                    "default": _profile(rootfs={"dir": "/srv/rootfs-a"}),
                    "online": _profile(rootfs={"dir": "/srv/rootfs-b"}, network=True),
                },
            }
        )
        if cfg.profiles["default"].rootfs.dir != "/srv/rootfs-a":
            raise AssertionError('profiles["default"].rootfs.dir == "/srv/rootfs-a"')
        if cfg.profiles["online"].isolation.network is not True:
            raise AssertionError('profiles["online"].isolation.network is True')

    def test_empty_registry_rejected(self) -> None:
        with pytest.raises(ValueError, match="profiles"):
            SandboxConfig.model_validate({"profiles": {}})

    def test_broken_profile_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_processes"):
            SandboxConfig.model_validate(
                {"profiles": {"bad": _raw_profile(max_processes=0)}}
            )


class TestToolProfile:
    """Инструмент получает готовый профиль ссылкой и ничего в нём не правит."""

    def test_profile_comes_from_reference(self) -> None:
        if _tool_config().profile.run.cwd != "/workspace":
            raise AssertionError('_tool_config().profile.run.cwd == "/workspace"')

    def test_reference_may_be_a_plain_mapping(self) -> None:
        """OmegaConf подставляет узел профиля как словарь."""
        raw = _raw_profile(ro=("/srv/b",))
        cfg = SandboxToolConfig.model_validate({"profile": raw})

        if cfg.profile.isolation.max_processes != 256:
            raise AssertionError("cfg.profile.isolation.max_processes == 256")
        if [b.host for b in cfg.profile.mounts.ro] != ["/srv/b"]:
            raise AssertionError("mounts.ro == [/srv/b]")

    def test_missing_profile_rejected(self) -> None:
        with pytest.raises(ValueError, match="profile"):
            SandboxToolConfig.model_validate({})


class TestProfileInheritance:
    """extends: профиль объявляется правкой другого, а не копией целиком."""

    def test_named_field_replaces_the_base_one(self) -> None:
        child = SandboxProfile.model_validate(
            {"extends": _raw_profile(), "isolation": {"network": True}}
        )

        if child.isolation.network is not True:
            raise AssertionError("child.isolation.network is True")

    def test_untouched_fields_come_from_the_base(self) -> None:
        """Правка одного поля группы не роняет остальные поля этой же группы."""
        base = _raw_profile(ro=("/srv/b",))
        child = SandboxProfile.model_validate(
            {"extends": base, "isolation": {"network": True}}
        )

        if child.isolation.max_processes != 256:
            raise AssertionError("child.isolation.max_processes == 256")
        if [b.host for b in child.mounts.ro] != ["/srv/b"]:
            raise AssertionError("mounts.ro == [/srv/b]")

    def test_untouched_groups_come_from_the_base(self) -> None:
        child = SandboxProfile.model_validate(
            {"extends": _raw_profile(), "run": {"cwd": "/tmp"}}  # noqa: S108
        )

        if child.limits.process_memory_bytes != 512 * 1024 * 1024:
            raise AssertionError("лимит памяти пришёл из базы")

    def test_base_is_not_mutated(self) -> None:
        base = _raw_profile()
        SandboxProfile.model_validate({"extends": base, "isolation": {"network": True}})

        if base["isolation"]["network"] is not False:
            raise AssertionError("база профиля осталась прежней")

    def test_chain_of_two_bases(self) -> None:
        middle = {"extends": _raw_profile(), "isolation": {"network": True}}
        child = SandboxProfile.model_validate(
            {"extends": middle, "run": {"cwd": "/tmp"}}  # noqa: S108
        )

        if child.isolation.network is not True:
            raise AssertionError("child.isolation.network is True")
        if child.run.cwd != "/tmp":  # noqa: S108
            raise AssertionError('child.run.cwd == "/tmp"')

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValueError, match="Extra inputs"):
            SandboxProfile.model_validate(
                {"extends": _raw_profile(), "нет-такой-группы": 1}
            )

    def test_invalid_value_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_processes"):
            SandboxProfile.model_validate(
                {"extends": _raw_profile(), "isolation": {"max_processes": 0}}
            )


class TestProfileReference:
    """Ссылка на профиль резолвится при загрузке конфига, а не при вызове."""

    @staticmethod
    def _raw(reference: str) -> DictConfig:
        return OmegaConf.create(
            {
                "sandbox": {"profiles": {"default": dict(_PROFILE_BASE)}},
                "tool": {"bash": {"sandbox": {"profile": reference}}},
            }
        )

    def test_reference_is_resolved(self) -> None:
        raw = self._raw("${sandbox.profiles.default}")
        cfg = bind(raw, path="tool.bash.sandbox", model=SandboxToolConfig)
        if cfg.profile.isolation.max_processes != 256:
            raise AssertionError("cfg.profile.isolation.max_processes == 256")

    def test_unknown_profile_fails_at_load(self) -> None:
        raw = self._raw("${sandbox.profiles.нет-такого}")
        with pytest.raises(InterpolationKeyError, match=r"sandbox\.profiles"):
            bind(raw, path="tool.bash.sandbox", model=SandboxToolConfig)


class TestComponentIsolation:
    def test_sandbox_imports_without_agent_tools(self) -> None:
        """Порядок импорта не должен ломать пакет: цикла быть не может."""
        code = (
            "import boba.sandbox as s\n"
            "if not (s.ZygoteToolCaller and s.SandboxToolConfig):\n"
            "    raise SystemExit('пакет собран без вызывателя или конфига')\n"
            "print('ok')\n"
        )
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip() != "ok":
            raise AssertionError('result.stdout.strip() == "ok"')
