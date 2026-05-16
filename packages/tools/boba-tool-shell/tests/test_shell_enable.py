"""Тесты enable-конвенции ShellPlugin + обязательность полей."""

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
def test_enabled_yields_bash(
    make_shell_tool_names: Callable[[str], list[str]],
    enabled_toml_body: str,
):
    assert make_shell_tool_names(enabled_toml_body) == ["bash"]


@pytest.mark.skipif(_HAS_BWRAP, reason="актуально только без bwrap")
def test_enabled_without_bwrap_raises(
    make_shell_tool_names: Callable[[str], list[str]],
    enabled_toml_body: str,
):
    with pytest.raises(RuntimeError, match="bwrap"):
        make_shell_tool_names(enabled_toml_body)


def test_enable_without_workspace_root_fails(
    make_shell_tool_names: Callable[[str], list[str]],
):
    body = (
        "enable = true\n"
        'default_profile = "default"\n'
        "[tool.shell.profiles.default]\n"
    )
    with pytest.raises(ValidationError, match="workspace_root"):
        make_shell_tool_names(body)


def test_enable_without_profiles_fails(
    make_shell_tool_names: Callable[[str], list[str]],
    tmp_path: Path,
):
    body = (
        "enable = true\n"
        f'workspace_root = "{tmp_path}"\n'
        'default_profile = "default"\n'
    )
    with pytest.raises(ValidationError, match="profiles"):
        make_shell_tool_names(body)


def test_enable_without_default_profile_fails(
    make_shell_tool_names: Callable[[str], list[str]],
    tmp_path: Path,
):
    body = (
        "enable = true\n"
        f'workspace_root = "{tmp_path}"\n'
        "[tool.shell.profiles.default]\n"
    )
    with pytest.raises(ValidationError, match="default_profile"):
        make_shell_tool_names(body)


def test_default_profile_must_exist_in_profiles(
    make_shell_tool_names: Callable[[str], list[str]],
    tmp_path: Path,
):
    body = (
        "enable = true\n"
        f'workspace_root = "{tmp_path}"\n'
        'default_profile = "missing"\n'
        "[tool.shell.profiles.default]\n"
    )
    with pytest.raises(ValidationError, match="отсутствует"):
        make_shell_tool_names(body)


def test_workspace_root_must_exist(
    make_shell_tool_names: Callable[[str], list[str]],
):
    body = (
        "enable = true\n"
        'workspace_root = "/no/such/dir/anywhere"\n'
        'default_profile = "default"\n'
        "[tool.shell.profiles.default]\n"
    )
    with pytest.raises(ValidationError, match="не существует"):
        make_shell_tool_names(body)
