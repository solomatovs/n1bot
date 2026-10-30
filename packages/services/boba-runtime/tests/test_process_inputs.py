"""Входы процесса: подкоманда и пути --config (конфиг экземпляра и наложения),
каталог программы из BOBA_RUNTIME и раскладка экземпляра."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pytest

from boba.config import ConfigError
from boba.runtime.config import (
    ConfigArguments,
    InstalledProgram,
    InstanceConfig,
    InstanceData,
    ProgramCommand,
    ProgramDir,
    ProgramRoots,
    SourceProgram,
)


def test_repeated_config_keeps_the_order_of_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv = [
        "prog",
        "serve",
        "--config",
        "/etc/boba/config.toml",
        "--config",
        "/srv/stand.toml",
    ]
    monkeypatch.setattr(sys, "argv", argv)

    call = ConfigArguments(argparse.ArgumentParser(prog="prog")).call()

    assert call.command is ProgramCommand.SERVE
    assert call.paths == (Path("/etc/boba/config.toml"), Path("/srv/stand.toml"))


def test_health_takes_the_same_config(monkeypatch: pytest.MonkeyPatch) -> None:
    argv = ["prog", "health", "--config", "/etc/boba/config.toml"]
    monkeypatch.setattr(sys, "argv", argv)

    call = ConfigArguments(argparse.ArgumentParser(prog="prog")).call()

    assert call.command is ProgramCommand.HEALTH
    assert call.paths == (Path("/etc/boba/config.toml"),)


def test_missing_command_stops_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["prog", "--config", "/etc/boba/config.toml"])

    with pytest.raises(SystemExit):
        ConfigArguments(argparse.ArgumentParser(prog="prog")).call()


def test_missing_config_stops_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["prog", "serve"])

    with pytest.raises(SystemExit):
        ConfigArguments(argparse.ArgumentParser(prog="prog")).call()


def test_interpreter_snapshot_leaves_the_working_directory_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    interpreter = ProgramRoots().locate().interpreter

    assert os.path.isabs(interpreter.executable)
    for root in interpreter.roots:
        assert not str(tmp_path).startswith(root.rstrip("/") + "/")
        assert root != str(tmp_path)


def test_installed_python_program_is_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Каталог без дерева исходников рядом — установленная программа:
    все части, и статика тоже, в нём самом."""
    monkeypatch.setenv(ProgramRoots.RUNTIME_ENV, str(tmp_path))

    program = ProgramRoots().locate()

    root = tmp_path.resolve()
    assert isinstance(program, InstalledProgram)
    assert program.root == root
    assert program.path(ProgramDir.SHARE) == root / "share"
    assert program.located("rootfs/base.ext4") == root / "rootfs/base.ext4"
    assert program.located("/srv/models") == Path("/srv/models")
    assert program.binaries().dirs[0] == str(root / "libexec")


def test_program_from_sources_takes_git_files_from_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """runtime/ в дереве исходников: статика чата — ассеты пакета, собранное
    (libexec, sandbox, rootfs, models) — из runtime/."""
    tree = tmp_path.resolve()
    (tree / "packages").mkdir()
    (tree / "runtime").mkdir()
    monkeypatch.setenv(ProgramRoots.RUNTIME_ENV, str(tree / "runtime"))

    program = ProgramRoots().locate()

    assert isinstance(program, SourceProgram)
    assert program.path(ProgramDir.SHARE) == tree / "packages/apps/boba-aid/assets"
    assert program.path(ProgramDir.LIBEXEC) == tree / "runtime/libexec"
    assert program.located("models/rapidocr") == tree / "runtime/models/rapidocr"
    assert program.workspace_template() == tree / "runtime/sandbox/workspace.ext4"


def test_program_from_sources_without_runtime_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ProgramRoots.RUNTIME_ENV, raising=False)

    with pytest.raises(ConfigError, match=ProgramRoots.RUNTIME_ENV):
        ProgramRoots().locate()


def test_instance_data_layout_is_fixed(tmp_path: Path) -> None:
    instance = InstanceConfig(data_dir=str(tmp_path), ca_file="/etc/ssl/ca.pem")
    layout = instance.layout(tmp_path / "conf")

    assert instance.data(InstanceData.TOOL_LOGS) == tmp_path / "tool-logs"
    assert layout.krb5_conf() == tmp_path / "conf" / "krb" / "krb5.conf"
    assert layout.kerberos().ccache_dir == str(tmp_path / "krb" / "cache")


def test_instance_paths_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute"):
        InstanceConfig(data_dir="data", ca_file="/etc/ssl/ca.pem")
