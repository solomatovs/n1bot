"""Компонент sandbox: реестр профилей и независимость от agent.tools."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from boba.chainlit2.sandbox import SandboxConfig, SandboxProfile

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
    "cwd": "",
}


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _profile(**kw: Any) -> SandboxProfile:
    return SandboxProfile.model_validate({**_PROFILE_BASE, **kw})


def _config(**kw: Any) -> SandboxConfig:
    profiles = {
        "default": _profile(cwd="/workspace"),
        "online": _profile(network=True),
    }
    fields: dict[str, Any] = {"profiles": profiles}
    fields.update(kw)
    return SandboxConfig.model_validate(fields)


class TestProfileRegistry:
    """Инструмент ссылается на профиль по имени, а не собирает окружение сам."""

    def test_profile_by_name(self) -> None:
        assert _config().profile("online").network is True

    def test_empty_name_is_rejected(self) -> None:
        """Профиля по умолчанию нет: имя обязано быть названо явно."""
        with pytest.raises(KeyError, match="profile name is required"):
            _config().profile("")

    def test_unknown_profile_lists_available(self) -> None:
        with pytest.raises(KeyError, match="available"):
            _config().profile("нет-такого")

    def test_empty_registry_rejected(self) -> None:
        with pytest.raises(ValueError, match="profiles"):
            SandboxConfig.model_validate({"profiles": {}})

    def test_profiles_may_differ_in_rootfs(self) -> None:
        cfg = SandboxConfig.model_validate(
            {
                "profiles": {
                    "a": _profile(rootfs="/srv/rootfs-a"),
                    "b": _profile(rootfs="/srv/rootfs-b", network=True),
                },
            }
        )
        assert cfg.profile("a").rootfs == "/srv/rootfs-a"
        assert cfg.profile("b").rootfs == "/srv/rootfs-b"


class TestToolProfileBinding:
    """Профиль инструмента проверяется на старте, а не при первом вызове."""

    def test_unknown_profile_fails_at_startup(self) -> None:
        from boba.chainlit2.agent.tools.sandbox.config import BashSandboxConfig

        with pytest.raises(ValueError, match="is not defined"):
            BashSandboxConfig(sandbox=_config(), profile="нет-такого")

    def test_known_profile_accepted(self) -> None:
        from boba.chainlit2.agent.tools.sandbox.config import BashSandboxConfig

        cfg = BashSandboxConfig(sandbox=_config(), profile="online")
        assert cfg.profile == "online"


class TestComponentIsolation:
    def test_sandbox_imports_without_agent_tools(self) -> None:
        """Порядок импорта не должен ломать пакет: цикла быть не может."""
        code = (
            "import boba.chainlit2.sandbox as s\n"
            "assert s.SandboxRunner and s.SandboxConfig\n"
            "print('ok')\n"
        )
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "ok"
