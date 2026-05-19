"""Тесты конфигов и `enable_if`-предикатов shell-tool'ов v2."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from boba.tool.shell.bash_local import _bash_local_enabled
from boba.tool.shell.bash_sandbox import _bash_sandbox_enabled
from boba.tool.shell.config import BashLocalConfig, BashSandboxConfig

_HAS_BWRAP = shutil.which("bwrap") is not None


# --- BashLocalConfig --------------------------------------------------------


def test_local_disabled_by_default(
    load_local_config: Callable[[str], BashLocalConfig],
):
    cfg = load_local_config("")
    assert cfg.enable is False
    assert _bash_local_enabled(cfg) is False


def test_local_disabled_explicit(
    load_local_config: Callable[[str], BashLocalConfig],
):
    cfg = load_local_config("enable = false\n")
    assert cfg.enable is False
    assert _bash_local_enabled(cfg) is False


def test_local_enabled_with_workspace(
    load_local_config: Callable[[str], BashLocalConfig],
    tmp_path: Path,
):
    cfg = load_local_config(
        f"enable = true\nworkspace_root = \"{tmp_path}\"\n",
    )
    assert cfg.enable is True
    assert _bash_local_enabled(cfg) is True
    assert cfg.workspace_root == tmp_path.resolve()


def test_local_enable_without_workspace_root_fails(
    load_local_config: Callable[[str], BashLocalConfig],
):
    with pytest.raises(ValidationError, match="workspace_root"):
        load_local_config("enable = true\n")


def test_local_workspace_must_exist(
    load_local_config: Callable[[str], BashLocalConfig],
):
    body = 'enable = true\nworkspace_root = "/no/such/dir/anywhere"\n'
    with pytest.raises(ValidationError, match="не существует"):
        load_local_config(body)


def test_local_timeout_below_min_rejected(
    load_local_config: Callable[[str], BashLocalConfig],
    tmp_path: Path,
):
    body = (
        f"enable = true\nworkspace_root = \"{tmp_path}\"\n"
        "timeout_sec = 0\n"
    )
    with pytest.raises(ValidationError, match="timeout_sec"):
        load_local_config(body)


def test_local_timeout_above_max_rejected(
    load_local_config: Callable[[str], BashLocalConfig],
    tmp_path: Path,
):
    body = (
        f"enable = true\nworkspace_root = \"{tmp_path}\"\n"
        "timeout_sec = 9999\n"
    )
    with pytest.raises(ValidationError, match="timeout_sec"):
        load_local_config(body)


def test_local_max_output_below_min_rejected(
    load_local_config: Callable[[str], BashLocalConfig],
    tmp_path: Path,
):
    body = (
        f"enable = true\nworkspace_root = \"{tmp_path}\"\n"
        "max_output_bytes = 512\n"
    )
    with pytest.raises(ValidationError, match="max_output_bytes"):
        load_local_config(body)


# --- BashSandboxConfig ------------------------------------------------------


def test_sandbox_disabled_by_default(
    load_sandbox_config: Callable[[str], BashSandboxConfig],
):
    cfg = load_sandbox_config("")
    assert cfg.enable is False
    assert _bash_sandbox_enabled(cfg) is False


@pytest.mark.skipif(not _HAS_BWRAP, reason="требуется bubblewrap")
def test_sandbox_enabled_full(
    load_sandbox_config: Callable[[str], BashSandboxConfig],
    tmp_path: Path,
):
    cfg = load_sandbox_config(
        f"enable = true\nworkspace_root = \"{tmp_path}\"\n"
        'default_profile = "default"\n'
        "[tool.bash_sandbox.profiles.default]\n",
    )
    assert cfg.enable is True
    assert _bash_sandbox_enabled(cfg) is True


@pytest.mark.skipif(_HAS_BWRAP, reason="актуально только без bwrap")
def test_sandbox_predicate_raises_without_bwrap(
    load_sandbox_config: Callable[[str], BashSandboxConfig],
    tmp_path: Path,
):
    cfg = load_sandbox_config(
        f"enable = true\nworkspace_root = \"{tmp_path}\"\n"
        'default_profile = "default"\n'
        "[tool.bash_sandbox.profiles.default]\n",
    )
    with pytest.raises(RuntimeError, match="bwrap"):
        _bash_sandbox_enabled(cfg)


def test_sandbox_enable_without_workspace_root_fails(
    load_sandbox_config: Callable[[str], BashSandboxConfig],
):
    body = (
        "enable = true\n"
        'default_profile = "default"\n'
        "[tool.bash_sandbox.profiles.default]\n"
    )
    with pytest.raises(ValidationError, match="workspace_root"):
        load_sandbox_config(body)


def test_sandbox_enable_without_profiles_fails(
    load_sandbox_config: Callable[[str], BashSandboxConfig],
    tmp_path: Path,
):
    body = (
        f"enable = true\nworkspace_root = \"{tmp_path}\"\n"
        'default_profile = "default"\n'
    )
    with pytest.raises(ValidationError, match="profiles"):
        load_sandbox_config(body)


def test_sandbox_enable_without_default_profile_fails(
    load_sandbox_config: Callable[[str], BashSandboxConfig],
    tmp_path: Path,
):
    body = (
        f"enable = true\nworkspace_root = \"{tmp_path}\"\n"
        "[tool.bash_sandbox.profiles.default]\n"
    )
    with pytest.raises(ValidationError, match="default_profile"):
        load_sandbox_config(body)


def test_sandbox_default_profile_outside_registry_rejected(
    load_sandbox_config: Callable[[str], BashSandboxConfig],
    tmp_path: Path,
):
    body = (
        f"enable = true\nworkspace_root = \"{tmp_path}\"\n"
        'default_profile = "missing"\n'
        "[tool.bash_sandbox.profiles.default]\n"
    )
    with pytest.raises(ValidationError, match="missing"):
        load_sandbox_config(body)


def test_sandbox_paths_get_canonicalized(tmp_path: Path):
    cfg = BashSandboxConfig.model_validate({
        "enable": True,
        "workspace_root": tmp_path,
        "default_profile": "default",
        "profiles": {
            "default": {
                # /lib часто symlink на debian-подобных; canonicalize
                # должен оставить абсолютный путь.
                "ro_binds": ["/lib"],
            },
        },
    })
    canonical = cfg.profiles["default"].ro_binds
    assert all(p.startswith("/") for p in canonical)
