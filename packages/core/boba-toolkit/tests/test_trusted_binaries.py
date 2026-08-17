"""Резолв бинарей: PATH не участвует, подменяемые пути отвергаются."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from boba.toolkit.binaries import (
    SandboxBinary,
    TrustedBinaries,
    UntrustedBinaryError,
)


def _executable(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


class TestResolve:
    def test_finds_binary_in_trusted_dir(self, tmp_path: Path) -> None:
        _executable(tmp_path, SandboxBinary.BWRAP.value)
        binaries = TrustedBinaries(dirs=(str(tmp_path),))

        found = binaries.resolve(SandboxBinary.BWRAP)

        if found != str(tmp_path / "bwrap"):
            raise AssertionError('found == str(tmp_path / "bwrap")')

    def test_path_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Главное свойство: подмена PATH не меняет запускаемый файл."""
        trusted = tmp_path / "trusted"
        attacker = tmp_path / "attacker"
        trusted.mkdir()
        attacker.mkdir()
        _executable(trusted, SandboxBinary.BWRAP.value)
        _executable(attacker, SandboxBinary.BWRAP.value)
        monkeypatch.setenv("PATH", f"{attacker}{os.pathsep}{trusted}")
        binaries = TrustedBinaries(dirs=(str(trusted),))

        found = binaries.resolve(SandboxBinary.BWRAP)

        if found != str(trusted / "bwrap"):
            raise AssertionError('found == str(trusted / "bwrap")')

    def test_dirs_are_searched_in_order(self, tmp_path: Path) -> None:
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        _executable(first, SandboxBinary.FUSE2FS.value)
        _executable(second, SandboxBinary.FUSE2FS.value)
        binaries = TrustedBinaries(dirs=(str(first), str(second)))

        if binaries.resolve(SandboxBinary.FUSE2FS) != str(first / "fuse2fs"):
            raise AssertionError("binaries.resolve(SandboxBinary.FUSE2FS) == str(firs…")

    def test_missing_binary_raises(self, tmp_path: Path) -> None:
        binaries = TrustedBinaries(dirs=(str(tmp_path),))

        with pytest.raises(UntrustedBinaryError, match="not found"):
            binaries.resolve(SandboxBinary.BWRAP)

    def test_absent_dir_is_skipped(self, tmp_path: Path) -> None:
        present = tmp_path / "present"
        present.mkdir()
        _executable(present, SandboxBinary.BWRAP.value)
        binaries = TrustedBinaries(dirs=(str(tmp_path / "absent"), str(present)))

        if binaries.resolve(SandboxBinary.BWRAP) != str(present / "bwrap"):
            raise AssertionError("binaries.resolve(SandboxBinary.BWRAP) == str(presen…")

    def test_non_executable_file_is_not_a_binary(self, tmp_path: Path) -> None:
        path = tmp_path / SandboxBinary.BWRAP.value
        path.write_text("")
        path.chmod(0o644)
        binaries = TrustedBinaries(dirs=(str(tmp_path),))

        with pytest.raises(UntrustedBinaryError, match="not found"):
            binaries.resolve(SandboxBinary.BWRAP)

    def test_resolve_any_falls_back_to_the_next_name(self, tmp_path: Path) -> None:
        _executable(tmp_path, SandboxBinary.FUSE2FS.value)
        binaries = TrustedBinaries(dirs=(str(tmp_path),))

        found = binaries.resolve_any(SandboxBinary.BWRAP, SandboxBinary.FUSE2FS)

        if found != str(tmp_path / SandboxBinary.FUSE2FS.value):
            raise AssertionError("found == str(tmp_path / SandboxBinary.FUSE2FS.value)")

    def test_has_reports_availability_without_raising(self, tmp_path: Path) -> None:
        binaries = TrustedBinaries(dirs=(str(tmp_path),))

        if binaries.has(SandboxBinary.BWRAP):
            raise AssertionError("not binaries.has(SandboxBinary.BWRAP)")

        _executable(tmp_path, SandboxBinary.BWRAP.value)

        if not (binaries.has(SandboxBinary.BWRAP)):
            raise AssertionError("binaries.has(SandboxBinary.BWRAP)")


class TestWritablePathsRejected:
    def test_world_writable_binary_rejected(self, tmp_path: Path) -> None:
        binary = _executable(tmp_path, SandboxBinary.BWRAP.value)
        binary.chmod(0o757)
        binaries = TrustedBinaries(dirs=(str(tmp_path),))

        with pytest.raises(UntrustedBinaryError, match="world-writable"):
            binaries.resolve(SandboxBinary.BWRAP)

    def test_group_writable_binary_rejected(self, tmp_path: Path) -> None:
        binary = _executable(tmp_path, SandboxBinary.BWRAP.value)
        binary.chmod(0o775)
        binaries = TrustedBinaries(dirs=(str(tmp_path),))

        with pytest.raises(UntrustedBinaryError, match="group-writable"):
            binaries.resolve(SandboxBinary.BWRAP)

    def test_world_writable_dir_rejected(self, tmp_path: Path) -> None:
        directory = tmp_path / "bin"
        directory.mkdir()
        _executable(directory, SandboxBinary.BWRAP.value)
        directory.chmod(0o777)
        binaries = TrustedBinaries(dirs=(str(directory),))

        with pytest.raises(UntrustedBinaryError, match="world-writable"):
            binaries.resolve(SandboxBinary.BWRAP)

    def test_parent_of_the_declared_dir_is_not_checked(self, tmp_path: Path) -> None:
        """Граница проверки: выше объявленного каталога отвечает развёртывание."""
        parent = tmp_path / "parent"
        directory = parent / "bin"
        directory.mkdir(parents=True)
        binary = _executable(directory, SandboxBinary.BWRAP.value)
        parent.chmod(0o777)
        binaries = TrustedBinaries(dirs=(str(directory),))

        if binaries.resolve(SandboxBinary.BWRAP) != str(binary):
            raise AssertionError("binaries.resolve(SandboxBinary.BWRAP) == str(binary)")

    def test_dir_declared_through_a_symlink_keeps_the_boundary(
        self, tmp_path: Path
    ) -> None:
        """Так объявлен release/current: граница — цель ссылки, не её родитель."""
        release = tmp_path / "boba-1.2.3"
        directory = release / "third" / "bin"
        directory.mkdir(parents=True)
        binary = _executable(directory, SandboxBinary.BWRAP.value)
        current = tmp_path / "current"
        current.symlink_to(release)
        release.chmod(0o775)
        declared = current / "third" / "bin"
        binaries = TrustedBinaries(dirs=(str(declared),))

        found = binaries.resolve(SandboxBinary.BWRAP)

        if found != str(declared / SandboxBinary.BWRAP.value):
            raise AssertionError("found == str(declared / SandboxBinary.BWRAP.value)")
        if Path(found).resolve() != binary:
            raise AssertionError("Path(found).resolve() == binary")

    def test_symlink_to_writable_target_rejected(self, tmp_path: Path) -> None:
        """Проверяется цель симлинка, а не сама ссылка."""
        store = tmp_path / "store"
        directory = tmp_path / "bin"
        store.mkdir()
        directory.mkdir()
        target = _executable(store, "real")
        target.chmod(0o777)
        link = directory / SandboxBinary.BWRAP.value
        link.symlink_to(target)
        binaries = TrustedBinaries(dirs=(str(directory),))

        with pytest.raises(UntrustedBinaryError, match="writable"):
            binaries.resolve(SandboxBinary.BWRAP)

    def test_sane_permissions_accepted(self, tmp_path: Path) -> None:
        directory = tmp_path / "bin"
        directory.mkdir()
        binary = _executable(directory, SandboxBinary.BWRAP.value)
        directory.chmod(0o755)

        binaries = TrustedBinaries(dirs=(str(directory),))

        if binaries.resolve(SandboxBinary.BWRAP) != str(binary):
            raise AssertionError("binaries.resolve(SandboxBinary.BWRAP) == str(binary)")
        if directory.stat().st_mode & stat.S_IWOTH:
            raise AssertionError("not directory.stat().st_mode & stat.S_IWOTH")


class TestConfig:
    def test_relative_dir_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be absolute"):
            TrustedBinaries(dirs=("relative/bin",))

    def test_empty_dirs_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 1 item"):
            TrustedBinaries(dirs=())

    def test_dirs_are_normalized(self) -> None:
        binaries = TrustedBinaries(dirs=("/usr/bin/",))

        if binaries.dirs != ("/usr/bin",):
            raise AssertionError('binaries.dirs == ("/usr/bin",)')
