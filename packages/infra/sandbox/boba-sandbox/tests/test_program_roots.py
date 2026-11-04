"""ProgramRoots: расположение программы и снимок интерпретатора."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from boba.config import ConfigError
from boba.sandbox.paths import ProgramDir, ProgramRoots, SourceProgram


def test_interpreter_snapshot_leaves_the_working_directory_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    interpreter = ProgramRoots().locate().interpreter

    assert os.path.isabs(interpreter.executable)
    for root in interpreter.roots:
        assert not str(tmp_path).startswith(root.rstrip("/") + "/")
        assert root != str(tmp_path)


def test_python_program_outside_the_sources_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Каталог без дерева исходников рядом: установленная программа только
    собранная, на python бывают лишь исходники."""
    monkeypatch.setenv(ProgramRoots.RUNTIME_ENV, str(tmp_path))

    with pytest.raises(ConfigError, match=r"runtime/ of the repository with packages/"):
        ProgramRoots().locate()


def test_program_from_sources_takes_git_files_from_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """runtime/ в дереве исходников: статика чата — ассеты пакета, собранное
    (libexec, sandbox, rootfs, models) — из runtime/."""
    tree = tmp_path.resolve()
    (tree / "packages").mkdir()
    (tree / "runtime").mkdir()
    monkeypatch.setenv(ProgramRoots.RUNTIME_ENV, str(tree / "runtime"))

    program = ProgramRoots(
        {ProgramDir.SHARE: Path("packages/apps/boba-aid/assets")}
    ).locate()

    assert isinstance(program, SourceProgram)
    assert program.path(ProgramDir.SHARE) == tree / "packages/apps/boba-aid/assets"
    assert program.path(ProgramDir.LIBEXEC) == tree / "runtime/libexec"
    assert program.located("models/rapidocr") == tree / "runtime/models/rapidocr"
    assert program.located("/srv/models") == Path("/srv/models")
    assert program.binaries().dirs[0] == str(tree / "runtime/libexec")
    assert program.workspace_template() == tree / "runtime/sandbox/workspace.ext4.gz"


def test_program_from_sources_without_runtime_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ProgramRoots.RUNTIME_ENV, raising=False)

    with pytest.raises(ConfigError, match=ProgramRoots.RUNTIME_ENV):
        ProgramRoots().locate()
