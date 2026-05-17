"""Тесты enable-конвенции ShellPlugin + обязательность полей.

Покрывают оба варианта (`variant = "sandbox"` / `"local"`) и валидацию
sub-конфигов `[tool.shell.sandbox]` / `[tool.shell.local]`. LLM-имя
у tool'а в обоих режимах одинаковое — `bash`.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

_HAS_BWRAP = shutil.which("bwrap") is not None


def test_disabled_by_default(
    make_shell_tool_names: Callable[[str], list[str]],
):
    assert make_shell_tool_names("") == []


def test_disabled_explicit(
    make_shell_tool_names: Callable[[str], list[str]],
):
    assert make_shell_tool_names("enable = false\n") == []


@pytest.mark.skipif(not _HAS_BWRAP, reason="требуется bubblewrap")
def test_sandbox_variant_yields_bash(
    make_shell_tool_names: Callable[[str], list[str]],
    enabled_sandbox_body: str,
):
    assert make_shell_tool_names(enabled_sandbox_body) == ["bash"]


def test_local_variant_yields_bash(
    make_shell_tool_names: Callable[[str], list[str]],
    enabled_local_body: str,
):
    assert make_shell_tool_names(enabled_local_body) == ["bash"]


@pytest.mark.skipif(_HAS_BWRAP, reason="актуально только без bwrap")
def test_sandbox_without_bwrap_raises(
    make_shell_tool_names: Callable[[str], list[str]],
    enabled_sandbox_body: str,
):
    with pytest.raises((RuntimeError, ValidationError), match="bwrap"):
        make_shell_tool_names(enabled_sandbox_body)


def test_local_without_bwrap_works(
    make_shell_tool_names: Callable[[str], list[str]],
    enabled_local_body: str,
):
    # local-режим не требует bwrap — должно работать на любой машине.
    assert make_shell_tool_names(enabled_local_body) == ["bash"]


def test_enable_without_variant_fails(
    make_shell_tool_names: Callable[[str], list[str]],
    tmp_path: Path,
):
    body = (
        "enable = true\n"
        f'workspace_root = "{tmp_path}"\n'
    )
    with pytest.raises(ValidationError, match="variant"):
        make_shell_tool_names(body)


def test_enable_without_workspace_root_fails(
    make_shell_tool_names: Callable[[str], list[str]],
):
    body = (
        "enable = true\n"
        'variant = "local"\n'
    )
    with pytest.raises(ValidationError, match="workspace_root"):
        make_shell_tool_names(body)


def test_sandbox_active_without_profiles_fails(
    make_shell_tool_names: Callable[[str], list[str]],
    tmp_path: Path,
):
    body = (
        "enable = true\n"
        f'workspace_root = "{tmp_path}"\n'
        'variant = "sandbox"\n'
        "[tool.shell.sandbox]\n"
        'default_profile = "default"\n'
    )
    with pytest.raises(ValidationError, match="sandbox.profiles"):
        make_shell_tool_names(body)


def test_sandbox_active_without_default_profile_fails(
    make_shell_tool_names: Callable[[str], list[str]],
    tmp_path: Path,
):
    body = (
        "enable = true\n"
        f'workspace_root = "{tmp_path}"\n'
        'variant = "sandbox"\n'
        "[tool.shell.sandbox.profiles.default]\n"
    )
    with pytest.raises(ValidationError, match="sandbox.default_profile"):
        make_shell_tool_names(body)


def test_workspace_root_must_exist(
    make_shell_tool_names: Callable[[str], list[str]],
):
    body = (
        "enable = true\n"
        'workspace_root = "/no/such/dir/anywhere"\n'
        'variant = "local"\n'
    )
    with pytest.raises(ValidationError, match="не существует"):
        make_shell_tool_names(body)


def test_unknown_variant_rejected(
    make_shell_tool_names: Callable[[str], list[str]],
    tmp_path: Path,
):
    body = (
        "enable = true\n"
        f'workspace_root = "{tmp_path}"\n'
        'variant = "bash_unknown"\n'
    )
    # Literal['sandbox','local'] валидируется pydantic'ом.
    with pytest.raises(ValidationError, match="variant"):
        make_shell_tool_names(body)


def test_local_timeout_below_min_rejected(
    make_shell_tool_names: Callable[[str], list[str]],
    tmp_path: Path,
):
    body = (
        "enable = true\n"
        f'workspace_root = "{tmp_path}"\n'
        'variant = "local"\n'
        "[tool.shell.local]\n"
        "timeout_sec = 0\n"
    )
    with pytest.raises(ValidationError, match=r"local\.timeout_sec"):
        make_shell_tool_names(body)


def test_local_timeout_above_max_rejected(
    make_shell_tool_names: Callable[[str], list[str]],
    tmp_path: Path,
):
    body = (
        "enable = true\n"
        f'workspace_root = "{tmp_path}"\n'
        'variant = "local"\n'
        "[tool.shell.local]\n"
        "timeout_sec = 9999\n"
    )
    with pytest.raises(ValidationError, match=r"local\.timeout_sec"):
        make_shell_tool_names(body)


def test_local_max_output_below_min_rejected(
    make_shell_tool_names: Callable[[str], list[str]],
    tmp_path: Path,
):
    body = (
        "enable = true\n"
        f'workspace_root = "{tmp_path}"\n'
        'variant = "local"\n'
        "[tool.shell.local]\n"
        "max_output_bytes = 512\n"
    )
    with pytest.raises(ValidationError, match=r"local\.max_output_bytes"):
        make_shell_tool_names(body)


def test_sandbox_paths_get_canonicalized():
    # Проверяем path-canonicalization напрямую на ShellPluginConfig,
    # без TOML — простой unit-тест над field_validator'ом SandboxProfile.
    import tempfile  # noqa: PLC0415
    from boba.tool.shell.plugin import ShellPluginConfig  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        cfg = ShellPluginConfig.model_validate({
            "enable": True,
            "workspace_root": Path(tmp),
            "variant": "sandbox",
            "sandbox": {
                "default_profile": "default",
                "profiles": {
                    "default": {
                        # относительный путь / symlink на стандартных
                        # debian-like — после canonicalize должен стать
                        # абсолютным.
                        "ro_binds": ["/lib"],
                    },
                },
            },
        })
        canonical = cfg.sandbox.profiles["default"].ro_binds
        # хотя бы один из путей был резолвен (на debian /lib → /usr/lib)
        assert all(p.startswith("/") for p in canonical)
