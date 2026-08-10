"""Компонент sandbox: профиль запуска инструмента и независимость от tools."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest
from omegaconf import DictConfig, OmegaConf
from omegaconf.errors import InterpolationKeyError

from boba.sandbox import SandboxConfig, SandboxProfile, SandboxToolConfig
from boba.settings import bind

_PROFILE_BASE: dict[str, Any] = {
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
    "cgroup_base": "",
    "oom_score_adj": 0,
    "cwd": "",
}


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _profile(**kw: Any) -> SandboxProfile:
    return SandboxProfile.model_validate({**_PROFILE_BASE, **kw})


def _tool_config(override: dict[str, Any], **profile_kw: Any) -> SandboxToolConfig:
    profile = _profile(cwd="/workspace", **profile_kw)
    return SandboxToolConfig.model_validate(
        {"profile": profile, "override": override}
    )


class TestProfileRegistry:
    """Секция [sandbox] валидируется на старте целиком, а не при первом вызове."""

    def test_profiles_are_parsed(self) -> None:
        cfg = SandboxConfig.model_validate(
            {
                "profiles": {
                    "default": _profile(rootfs="/srv/rootfs-a"),
                    "online": _profile(rootfs="/srv/rootfs-b", network=True),
                },
            }
        )
        assert cfg.profiles["default"].rootfs == "/srv/rootfs-a"
        assert cfg.profiles["online"].network is True

    def test_empty_registry_rejected(self) -> None:
        with pytest.raises(ValueError, match="profiles"):
            SandboxConfig.model_validate({"profiles": {}})

    def test_broken_profile_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_processes"):
            SandboxConfig.model_validate(
                {"profiles": {"bad": {**_PROFILE_BASE, "max_processes": 0}}}
            )


class TestToolProfile:
    """Инструмент получает профиль ссылкой; переопределения пишет админ."""

    def test_profile_comes_from_reference(self) -> None:
        assert _tool_config({}).effective().cwd == "/workspace"

    def test_reference_may_be_a_plain_mapping(self) -> None:
        """OmegaConf подставляет узел профиля как словарь."""
        cfg = SandboxToolConfig.model_validate(
            {"profile": dict(_PROFILE_BASE), "override": {}}
        )
        assert cfg.effective().max_processes == 256

    def test_field_replaces_base(self) -> None:
        assert _tool_config({"cwd": "/other"}).effective().cwd == "/other"

    def test_untouched_fields_come_from_base(self) -> None:
        assert _tool_config({"cwd": "/other"}).effective().max_processes == 256

    def test_mounts_are_parsed_from_strings(self) -> None:
        eff = _tool_config({"ro_binds": ["/srv/payload:/opt/payload"]}).effective()
        assert (eff.ro_binds[0].host, eff.ro_binds[0].target) == (
            "/srv/payload",
            "/opt/payload",
        )

    def test_list_is_replaced_not_appended(self) -> None:
        cfg = _tool_config({"ro_binds": ["/srv/b"]}, ro_binds=("/srv/a",))
        assert [b.host for b in cfg.effective().ro_binds] == ["/srv/b"]

    def test_any_field_may_be_overridden(self) -> None:
        """Ограничений нет: решает администратор."""
        eff = _tool_config({"network": True, "rootfs": "/srv/other"}).effective()
        assert eff.network is True
        assert eff.rootfs == "/srv/other"

    def test_base_profile_is_not_mutated(self) -> None:
        cfg = _tool_config({"network": True})
        cfg.effective()
        assert cfg.profile.network is False

    def test_empty_override_returns_base(self) -> None:
        cfg = _tool_config({})
        assert cfg.effective() is cfg.profile

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValueError, match="Extra inputs"):
            _tool_config({"нет-такого-поля": 1}).effective()

    def test_invalid_value_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_processes"):
            _tool_config({"max_processes": 0}).effective()

    def test_missing_profile_rejected(self) -> None:
        with pytest.raises(ValueError, match="profile"):
            SandboxToolConfig.model_validate({"override": {}})


class TestProfileReference:
    """Ссылка на профиль резолвится при загрузке конфига, а не при вызове."""

    @staticmethod
    def _raw(reference: str) -> DictConfig:
        return OmegaConf.create(
            {
                "sandbox": {"profiles": {"default": dict(_PROFILE_BASE)}},
                "tool": {"bash": {"sandbox": {"profile": reference, "override": {}}}},
            }
        )

    def test_reference_is_resolved(self) -> None:
        raw = self._raw("${sandbox.profiles.default}")
        cfg = bind(raw, path="tool.bash.sandbox", model=SandboxToolConfig)
        assert cfg.effective().max_processes == 256

    def test_unknown_profile_fails_at_load(self) -> None:
        raw = self._raw("${sandbox.profiles.нет-такого}")
        with pytest.raises(InterpolationKeyError, match=r"sandbox\.profiles"):
            bind(raw, path="tool.bash.sandbox", model=SandboxToolConfig)


class TestComponentIsolation:
    def test_sandbox_imports_without_agent_tools(self) -> None:
        """Порядок импорта не должен ломать пакет: цикла быть не может."""
        code = (
            "import boba.sandbox as s\n"
            "assert s.SandboxRunner and s.SandboxToolConfig\n"
            "print('ok')\n"
        )
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "ok"
