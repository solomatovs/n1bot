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
    WorkspaceError,
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


class TestCwdAwareResolve:
    """Сейчас cwd всегда ``/`` — существующее поведение не меняется.

    Когда появится ``cd``, эти тесты дополнятся кейсами с непустым cwd.
    """

    def test_cwd_defaults_to_root(self, service: FsWorkspaceService) -> None:
        assert service.cwd == "/"

    def test_relative_path_resolves_from_cwd(
        self, service: FsWorkspaceService, workspace_root: Path
    ) -> None:
        service._cwd_parts = ("docs", "api")
        resolved = service._resolve("spec.md")
        assert resolved.relative == "docs/api/spec.md"
        assert resolved.absolute == workspace_root / "docs/api/spec.md"

    def test_absolute_path_ignores_cwd(
        self, service: FsWorkspaceService, workspace_root: Path
    ) -> None:
        service._cwd_parts = ("docs", "api")
        resolved = service._resolve("/readme.md")
        assert resolved.relative == "readme.md"
        assert resolved.absolute == workspace_root / "readme.md"

    def test_parent_pops_cwd_before_clamp(
        self, service: FsWorkspaceService, workspace_root: Path
    ) -> None:
        service._cwd_parts = ("docs", "api")
        resolved = service._resolve("../../other.md")
        assert resolved.relative == "other.md"
        assert resolved.absolute == workspace_root / "other.md"

    def test_escape_from_cwd_clamped_at_root(
        self, service: FsWorkspaceService, workspace_root: Path
    ) -> None:
        service._cwd_parts = ("docs",)
        resolved = service._resolve("../../../etc/passwd")
        assert resolved.relative == "etc/passwd"
        assert resolved.absolute == workspace_root / "etc/passwd"


class TestCd:
    """Смена cwd через публичный ``cd``: валидация + ошибки."""

    def test_cd_to_existing_dir_updates_cwd(
        self, service: FsWorkspaceService, workspace_root: Path
    ) -> None:
        (workspace_root / "docs" / "api").mkdir(parents=True)
        service.cd("/docs/api")
        assert service.cwd == "/docs/api"

    def test_cd_relative_appends_to_cwd(
        self, service: FsWorkspaceService, workspace_root: Path
    ) -> None:
        (workspace_root / "docs" / "api").mkdir(parents=True)
        service.cd("docs")
        service.cd("api")
        assert service.cwd == "/docs/api"

    def test_cd_dotdot_pops_from_cwd(
        self, service: FsWorkspaceService, workspace_root: Path
    ) -> None:
        (workspace_root / "docs" / "api").mkdir(parents=True)
        service.cd("/docs/api")
        service.cd("..")
        assert service.cwd == "/docs"

    def test_cd_root_resets_cwd(
        self, service: FsWorkspaceService, workspace_root: Path
    ) -> None:
        (workspace_root / "docs").mkdir()
        service.cd("/docs")
        service.cd("/")
        assert service.cwd == "/"

    def test_cd_to_missing_dir_raises_not_found_and_keeps_cwd(
        self, service: FsWorkspaceService
    ) -> None:
        with pytest.raises(WorkspaceNotFoundError) as exc:
            service.cd("/does/not/exist")
        assert exc.value.path == "does/not/exist"
        assert service.cwd == "/"

    def test_cd_to_file_raises_error_and_keeps_cwd(
        self, service: FsWorkspaceService, workspace_root: Path
    ) -> None:
        (workspace_root / "file.txt").write_text("x")
        with pytest.raises(WorkspaceError) as exc:
            service.cd("/file.txt")
        assert not isinstance(exc.value, WorkspaceNotFoundError)
        assert "not a directory" in str(exc.value)
        assert exc.value.path == "file.txt"
        assert service.cwd == "/"

    def test_cd_escape_clamps_but_must_exist(
        self, service: FsWorkspaceService
    ) -> None:
        with pytest.raises(WorkspaceNotFoundError):
            service.cd("../../etc")
        assert service.cwd == "/"

    def test_other_ops_resolve_relative_to_cwd_after_cd(
        self, service: FsWorkspaceService, workspace_root: Path
    ) -> None:
        (workspace_root / "docs").mkdir()
        service.cd("/docs")
        with service.write_text("note.md") as f:
            f.write("hi")
        assert (workspace_root / "docs" / "note.md").read_text() == "hi"


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
