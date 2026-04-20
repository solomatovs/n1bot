"""Тесты нормализации путей :class:`FsWorkspaceService`.

Workspace — корневая директория для всех операций. Проверяем
4 сценария:

1. абсолютный путь ``/root`` → физически ``{workspace}/root``;
2. относительный ``./my/files`` → ``{workspace}/my/files``;
3. относительный ``my/files/large_file`` → ``{workspace}/my/files/large_file``;
4. попытка выйти за корень ``../../my/files/large_file`` — ``..`` клампятся,
   остаётся ``my/files/large_file`` под корнем; файла нет —
   :class:`WorkspaceNotFoundError`.

Дополнительно: ошибки адресуют пользователя относительным путём (реальный
путь на диск никогда не утекает наружу).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boba.adapters.fs_workspace import (
    FsPathValidator,
    FsWorkspaceService,
    _clamp_to_workspace,
    _ResolvedPath,
)
from boba.domain.core.workspace import (
    WorkspaceId,
    WorkspaceNotFoundError,
)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def service(workspace_root: Path) -> FsWorkspaceService:
    return FsWorkspaceService(
        workspace_id=WorkspaceId.new(),
        root=workspace_root,
    )


class TestClamp:
    """Чистая функция нормализации — без диска."""

    def test_absolute_stripped(self) -> None:
        assert _clamp_to_workspace("/root") == "root"

    def test_dot_relative(self) -> None:
        assert _clamp_to_workspace("./my/files") == "my/files"

    def test_plain_relative(self) -> None:
        assert _clamp_to_workspace("my/files/large_file") == "my/files/large_file"

    def test_parent_clamped_to_root(self) -> None:
        assert (
            _clamp_to_workspace("../../my/files/large_file")
            == "my/files/large_file"
        )

    def test_all_parents_collapse_to_empty(self) -> None:
        assert _clamp_to_workspace("../../..") == ""

    def test_mid_path_parent_pops_stack(self) -> None:
        assert _clamp_to_workspace("a/b/../c") == "a/c"


class TestFsPathValidator:
    """Легаси-интерфейс ``Validator[str]`` — возвращает абсолютный путь."""

    def test_absolute_input_stays_in_workspace(self, workspace_root: Path) -> None:
        v = FsPathValidator(workspace_root)
        assert v.validate("/root") == str(workspace_root / "root")

    def test_relative_dot_prefix(self, workspace_root: Path) -> None:
        v = FsPathValidator(workspace_root)
        assert v.validate("./my/files") == str(workspace_root / "my/files")

    def test_plain_relative(self, workspace_root: Path) -> None:
        v = FsPathValidator(workspace_root)
        assert (
            v.validate("my/files/large_file")
            == str(workspace_root / "my/files/large_file")
        )

    def test_escape_attempt_clamped(self, workspace_root: Path) -> None:
        v = FsPathValidator(workspace_root)
        assert (
            v.validate("../../my/files/large_file")
            == str(workspace_root / "my/files/large_file")
        )


class TestServiceResolve:
    """Все 4 пользовательских сценария на уровне сервиса."""

    def test_case_1_absolute_path(
        self, service: FsWorkspaceService, workspace_root: Path
    ) -> None:
        resolved = service._resolve("/root")
        assert isinstance(resolved, _ResolvedPath)
        assert resolved.source == "/root"
        assert resolved.relative == "root"
        assert resolved.absolute == workspace_root / "root"

    def test_case_2_dot_relative(
        self, service: FsWorkspaceService, workspace_root: Path
    ) -> None:
        resolved = service._resolve("./my/files")
        assert resolved.source == "./my/files"
        assert resolved.relative == "my/files"
        assert resolved.absolute == workspace_root / "my/files"

    def test_case_3_plain_relative(
        self, service: FsWorkspaceService, workspace_root: Path
    ) -> None:
        resolved = service._resolve("my/files/large_file")
        assert resolved.source == "my/files/large_file"
        assert resolved.relative == "my/files/large_file"
        assert resolved.absolute == workspace_root / "my/files/large_file"

    def test_case_4_escape_clamped(
        self, service: FsWorkspaceService, workspace_root: Path
    ) -> None:
        resolved = service._resolve("../../my/files/large_file")
        assert resolved.source == "../../my/files/large_file"
        assert resolved.relative == "my/files/large_file"
        assert resolved.absolute == workspace_root / "my/files/large_file"
        assert resolved.absolute.is_relative_to(workspace_root)


class TestServiceErrorLeakage:
    """Ошибки пользователю адресуют относительным путём, не абсолютным."""

    def test_case_4_read_missing_file_raises_not_found(
        self, service: FsWorkspaceService, workspace_root: Path
    ) -> None:
        source = "../../my/files/large_file"
        with pytest.raises(WorkspaceNotFoundError) as exc_info:
            service.read_text(source)
        assert exc_info.value.path == "my/files/large_file"
        assert str(workspace_root) not in str(exc_info.value)
        assert str(workspace_root) not in (exc_info.value.path or "")

    def test_delete_missing_uses_relative_path(
        self, service: FsWorkspaceService, workspace_root: Path
    ) -> None:
        with pytest.raises(WorkspaceNotFoundError) as exc_info:
            service.delete("/does/not/exist")
        assert exc_info.value.path == "does/not/exist"
        assert str(workspace_root) not in str(exc_info.value)


class TestServiceHappyPath:
    """Реальные операции чтения/записи через нормализованные пути."""

    def test_write_then_read_same_file_by_absolute_and_relative(
        self, service: FsWorkspaceService, workspace_root: Path
    ) -> None:
        with service.write_text("/note.txt") as f:
            f.write("hi")
        with service.read_text("./note.txt") as f:
            assert f.read() == "hi"
        assert (workspace_root / "note.txt").read_text() == "hi"

    def test_escape_and_clean_reach_same_file(
        self, service: FsWorkspaceService, workspace_root: Path
    ) -> None:
        with service.write_text("a/b/c.txt") as f:
            f.write("x")
        with service.read_text("../../a/b/c.txt") as f:
            assert f.read() == "x"
