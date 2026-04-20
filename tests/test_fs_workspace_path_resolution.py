"""Тесты нормализации путей :class:`FsProjectWorkspaceShell`.

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
    FsProjectWorkspaceShell,
    WorkspacePath,
    _contain_within_root,
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
def service(workspace_root: Path) -> FsProjectWorkspaceShell:
    return FsProjectWorkspaceShell(
        workspace_id=WorkspaceId.new(),
        root=workspace_root,
    )


class TestClamp:
    """Чистая функция нормализации — без диска."""

    def test_absolute_stripped(self) -> None:
        assert _contain_within_root("/root") == "root"

    def test_dot_relative(self) -> None:
        assert _contain_within_root("./my/files") == "my/files"

    def test_plain_relative(self) -> None:
        assert _contain_within_root("my/files/large_file") == "my/files/large_file"

    def test_parent_clamped_to_root(self) -> None:
        assert (
            _contain_within_root("../../my/files/large_file") == "my/files/large_file"
        )

    def test_all_parents_collapse_to_empty(self) -> None:
        assert _contain_within_root("../../..") == ""

    def test_mid_path_parent_pops_stack(self) -> None:
        assert _contain_within_root("a/b/../c") == "a/c"


class TestServiceResolve:
    """Все 4 пользовательских сценария на уровне сервиса."""

    def test_case_1_absolute_path(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        resolved = service._resolve("/root")
        assert isinstance(resolved, WorkspacePath)
        assert resolved.source == "/root"
        assert resolved.relative == "root"
        assert resolved.absolute == workspace_root / "root"

    def test_case_2_dot_relative(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        resolved = service._resolve("./my/files")
        assert resolved.source == "./my/files"
        assert resolved.relative == "my/files"
        assert resolved.absolute == workspace_root / "my/files"

    def test_case_3_plain_relative(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        resolved = service._resolve("my/files/large_file")
        assert resolved.source == "my/files/large_file"
        assert resolved.relative == "my/files/large_file"
        assert resolved.absolute == workspace_root / "my/files/large_file"

    def test_case_4_escape_clamped(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        resolved = service._resolve("../../my/files/large_file")
        assert resolved.source == "../../my/files/large_file"
        assert resolved.relative == "my/files/large_file"
        assert resolved.absolute == workspace_root / "my/files/large_file"
        assert resolved.absolute.is_relative_to(workspace_root)


class TestServiceErrorLeakage:
    """Ошибки пользователю адресуют относительным путём, не абсолютным."""

    def test_case_4_read_missing_file_raises_not_found(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        source = "../../my/files/large_file"
        with pytest.raises(WorkspaceNotFoundError) as exc_info:
            service.read_text(source)
        assert exc_info.value.path == "my/files/large_file"
        assert str(workspace_root) not in str(exc_info.value)
        assert str(workspace_root) not in (exc_info.value.path or "")

    def test_delete_missing_uses_relative_path(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        with pytest.raises(WorkspaceNotFoundError) as exc_info:
            service.delete("/does/not/exist")
        assert exc_info.value.path == "does/not/exist"
        assert str(workspace_root) not in str(exc_info.value)


class TestCwdAwareResolve:
    """Сейчас cwd всегда ``/`` — существующее поведение не меняется.

    Когда появится ``cd``, эти тесты дополнятся кейсами с непустым cwd.
    """

    def test_cwd_defaults_to_root(self, service: FsProjectWorkspaceShell) -> None:
        assert service.cwd == "/"

    def test_relative_path_resolves_from_cwd(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        service._cwd_parts = ("docs", "api")
        resolved = service._resolve("spec.md")
        assert resolved.relative == "docs/api/spec.md"
        assert resolved.absolute == workspace_root / "docs/api/spec.md"

    def test_absolute_path_ignores_cwd(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        service._cwd_parts = ("docs", "api")
        resolved = service._resolve("/readme.md")
        assert resolved.relative == "readme.md"
        assert resolved.absolute == workspace_root / "readme.md"

    def test_parent_pops_cwd_before_clamp(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        service._cwd_parts = ("docs", "api")
        resolved = service._resolve("../../other.md")
        assert resolved.relative == "other.md"
        assert resolved.absolute == workspace_root / "other.md"

    def test_escape_from_cwd_clamped_at_root(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        service._cwd_parts = ("docs",)
        resolved = service._resolve("../../../etc/passwd")
        assert resolved.relative == "etc/passwd"
        assert resolved.absolute == workspace_root / "etc/passwd"


class TestCd:
    """Смена cwd через публичный ``cd``: валидация + ошибки."""

    def test_cd_to_existing_dir_updates_cwd(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "docs" / "api").mkdir(parents=True)
        service.cd("/docs/api")
        assert service.cwd == "/docs/api"

    def test_cd_relative_appends_to_cwd(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "docs" / "api").mkdir(parents=True)
        service.cd("docs")
        service.cd("api")
        assert service.cwd == "/docs/api"

    def test_cd_dotdot_pops_from_cwd(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "docs" / "api").mkdir(parents=True)
        service.cd("/docs/api")
        service.cd("..")
        assert service.cwd == "/docs"

    def test_cd_root_resets_cwd(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "docs").mkdir()
        service.cd("/docs")
        service.cd("/")
        assert service.cwd == "/"

    def test_cd_to_missing_dir_raises_not_found_and_keeps_cwd(
        self, service: FsProjectWorkspaceShell
    ) -> None:
        with pytest.raises(WorkspaceNotFoundError) as exc:
            service.cd("/does/not/exist")
        assert exc.value.path == "does/not/exist"
        assert service.cwd == "/"

    def test_cd_to_file_raises_error_and_keeps_cwd(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "file.txt").write_text("x")
        with pytest.raises(WorkspaceError) as exc:
            service.cd("/file.txt")
        assert not isinstance(exc.value, WorkspaceNotFoundError)
        assert "not a directory" in str(exc.value)
        assert exc.value.path == "file.txt"
        assert service.cwd == "/"

    def test_cd_escape_clamps_but_must_exist(
        self, service: FsProjectWorkspaceShell
    ) -> None:
        with pytest.raises(WorkspaceNotFoundError):
            service.cd("../../etc")
        assert service.cwd == "/"

    def test_other_ops_resolve_relative_to_cwd_after_cd(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "docs").mkdir()
        service.cd("/docs")
        with service.write_text("note.md") as f:
            f.write("hi")
        assert (workspace_root / "docs" / "note.md").read_text() == "hi"


class TestMkdir:
    """Создание директории через сервис."""

    def test_mkdir_creates_nested(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        service.mkdir("docs/api")
        assert (workspace_root / "docs" / "api").is_dir()

    def test_mkdir_existing_is_noop(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "docs").mkdir()
        service.mkdir("docs")
        assert (workspace_root / "docs").is_dir()

    def test_mkdir_over_file_raises(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "file.txt").write_text("x")
        with pytest.raises(WorkspaceError) as exc:
            service.mkdir("file.txt")
        assert exc.value.path == "file.txt"

    def test_mkdir_relative_to_cwd(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "docs").mkdir()
        service.cd("/docs")
        service.mkdir("api")
        assert (workspace_root / "docs" / "api").is_dir()

    def test_mkdir_escape_clamped(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        service.mkdir("../../evil")
        assert (workspace_root / "evil").is_dir()


class TestRm:
    """Удаление файлов и директорий (rm / rm -r)."""

    def test_rm_file(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("x")
        service.delete("a.txt")
        assert not (workspace_root / "a.txt").exists()

    def test_rm_missing_raises_not_found(
        self, service: FsProjectWorkspaceShell
    ) -> None:
        with pytest.raises(WorkspaceNotFoundError) as exc:
            service.delete("ghost.txt")
        assert exc.value.path == "ghost.txt"

    def test_rm_dir_without_recursive_fails(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "docs").mkdir()
        with pytest.raises(WorkspaceError) as exc:
            service.delete("docs")
        assert not isinstance(exc.value, WorkspaceNotFoundError)
        assert "is a directory" in str(exc.value)
        assert exc.value.path == "docs"
        assert (workspace_root / "docs").is_dir()

    def test_rm_dir_recursive(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "docs" / "api").mkdir(parents=True)
        (workspace_root / "docs" / "api" / "file.md").write_text("x")
        service.delete("docs", recursive=True)
        assert not (workspace_root / "docs").exists()


class TestMv:
    """Перемещение/переименование."""

    def test_mv_rename_file(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("x")
        service.move("a.txt", "b.txt")
        assert not (workspace_root / "a.txt").exists()
        assert (workspace_root / "b.txt").read_text() == "x"

    def test_mv_into_existing_dir(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("x")
        (workspace_root / "docs").mkdir()
        service.move("a.txt", "docs")
        assert (workspace_root / "docs" / "a.txt").read_text() == "x"

    def test_mv_missing_src_raises_not_found(
        self, service: FsProjectWorkspaceShell
    ) -> None:
        with pytest.raises(WorkspaceNotFoundError) as exc:
            service.move("ghost.txt", "b.txt")
        assert exc.value.path == "ghost.txt"

    def test_mv_overwrites_existing_file(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("new")
        (workspace_root / "b.txt").write_text("old")
        service.move("a.txt", "b.txt")
        assert (workspace_root / "b.txt").read_text() == "new"

    def test_mv_absolute_paths(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("x")
        service.move("/a.txt", "/b.txt")
        assert (workspace_root / "b.txt").read_text() == "x"

    def test_mv_respects_cwd(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "docs").mkdir()
        (workspace_root / "docs" / "a.txt").write_text("x")
        service.cd("/docs")
        service.move("a.txt", "b.txt")
        assert (workspace_root / "docs" / "b.txt").read_text() == "x"


class TestCatStreamingRange:
    """CatTool должен стримить строки, а не грузить файл целиком."""

    def test_range_returns_only_requested_lines(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        from boba.adapters.tools.cat import CatArgs, CatTool

        (workspace_root / "a.txt").write_text("a\nb\nc\nd\ne\n")
        tool = CatTool(service)
        result = tool.execute(None, CatArgs("a.txt", "utf-8", 2, 4))
        assert result.content == "### a.txt:2-4\n\nb\nc\nd"

    def test_range_beyond_end_reports_actual_last(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        from boba.adapters.tools.cat import CatArgs, CatTool

        (workspace_root / "a.txt").write_text("a\nb\nc\n")
        tool = CatTool(service)
        result = tool.execute(None, CatArgs("a.txt", "utf-8", 1, 100))
        assert result.content == "### a.txt:1-3\n\na\nb\nc"

    def test_range_stops_reading_after_end(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        """Хвост за ``end_line`` не читается — проверяем через счётчик."""
        from io import StringIO

        from boba.adapters.tools.cat import CatTool

        class CountingIO(StringIO):
            def __init__(self, s: str) -> None:
                super().__init__(s)
                self.lines_consumed = 0

            def __next__(self) -> str:  # pragma: no cover — делегирует
                self.lines_consumed += 1
                return super().__next__()

        fake = CountingIO("1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n")
        text, last = CatTool._read_range(fake, start=2, end=3)
        assert text == "2\n3"
        assert last == 3
        assert fake.lines_consumed == 4  # прочитали 1,2,3, затем 4 → break

    def test_ranged_read_exceeding_limit_raises_typed_error(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        from boba.adapters.tools import cat as cat_mod
        from boba.adapters.tools.cat import CatArgs, CatTool
        from boba.domain.core.tools import ToolOutputTooLargeError

        orig = cat_mod._MAX_LINES
        cat_mod._MAX_LINES = 2
        try:
            (workspace_root / "big.txt").write_text("1\n2\n3\n4\n5\n6\n")
            tool = CatTool(service)
            with pytest.raises(ToolOutputTooLargeError) as exc:
                tool.execute(None, CatArgs("big.txt", "utf-8", 1, 5))
            assert exc.value.limit == 2
            assert exc.value.unit == "строк"
            assert "1-5" in exc.value.hint
            assert "start_line=1" in exc.value.hint
            assert "end_line=2" in exc.value.hint
        finally:
            cat_mod._MAX_LINES = orig


class TestStat:
    """Метаданные через сервис и StatTool."""

    def test_meta_file(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("hello")
        meta = service.meta("a.txt")
        assert meta.path == "a.txt"
        assert meta.kind == "file"
        assert meta.size == 5

    def test_meta_directory(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "docs").mkdir()
        meta = service.meta("docs")
        assert meta.path == "docs"
        assert meta.kind == "directory"

    def test_meta_missing_raises_not_found(
        self, service: FsProjectWorkspaceShell
    ) -> None:
        with pytest.raises(WorkspaceNotFoundError) as exc:
            service.meta("ghost.txt")
        assert exc.value.path == "ghost.txt"

    def test_meta_path_is_relative_to_workspace(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        """Абсолютный путь на диске не должен утекать в path."""
        (workspace_root / "a.txt").write_text("x")
        meta = service.meta("/a.txt")
        assert meta.path == "a.txt"
        assert str(workspace_root) not in meta.path

    def test_stat_tool_returns_relative_path(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        from boba.adapters.tools.stat import StatArgs, StatTool

        (workspace_root / "docs").mkdir()
        (workspace_root / "docs" / "a.txt").write_text("hi")
        service.cd("/docs")
        tool = StatTool(service)
        result = tool.execute(None, StatArgs("a.txt"))
        assert "path: docs/a.txt" in result.content
        assert "kind: file" in result.content
        assert "size: 2" in result.content
        assert str(workspace_root) not in result.content


class TestCp:
    """Копирование файлов и директорий (cp / cp -r)."""

    def test_cp_file_to_new_path(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_bytes(b"hello")
        service.copy("a.txt", "b.txt")
        assert (workspace_root / "a.txt").read_bytes() == b"hello"
        assert (workspace_root / "b.txt").read_bytes() == b"hello"

    def test_cp_file_into_existing_dir(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_bytes(b"x")
        (workspace_root / "docs").mkdir()
        service.copy("a.txt", "docs")
        assert (workspace_root / "docs" / "a.txt").read_bytes() == b"x"
        assert (workspace_root / "a.txt").exists()

    def test_cp_overwrites_existing_file(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_bytes(b"new")
        (workspace_root / "b.txt").write_bytes(b"old")
        service.copy("a.txt", "b.txt")
        assert (workspace_root / "b.txt").read_bytes() == b"new"

    def test_cp_missing_src_raises_not_found(
        self, service: FsProjectWorkspaceShell
    ) -> None:
        with pytest.raises(WorkspaceNotFoundError) as exc:
            service.copy("ghost.txt", "b.txt")
        assert exc.value.path == "ghost.txt"

    def test_cp_dir_without_recursive_fails(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "src").mkdir()
        with pytest.raises(WorkspaceError) as exc:
            service.copy("src", "dst")
        assert not isinstance(exc.value, WorkspaceNotFoundError)
        assert "is a directory" in str(exc.value)
        assert exc.value.path == "src"
        assert not (workspace_root / "dst").exists()

    def test_cp_dir_recursive(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "src" / "nested").mkdir(parents=True)
        (workspace_root / "src" / "a.txt").write_bytes(b"a")
        (workspace_root / "src" / "nested" / "b.txt").write_bytes(b"b")
        service.copy("src", "dst", recursive=True)
        assert (workspace_root / "dst" / "a.txt").read_bytes() == b"a"
        assert (workspace_root / "dst" / "nested" / "b.txt").read_bytes() == b"b"
        assert (workspace_root / "src" / "a.txt").exists()

    def test_cp_respects_cwd(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "docs").mkdir()
        (workspace_root / "docs" / "a.txt").write_bytes(b"x")
        service.cd("/docs")
        service.copy("a.txt", "b.txt")
        assert (workspace_root / "docs" / "b.txt").read_bytes() == b"x"


class TestTouch:
    """Создание файла / обновление mtime."""

    def test_touch_creates_empty_file(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        service.touch("new.txt")
        assert (workspace_root / "new.txt").is_file()
        assert (workspace_root / "new.txt").read_bytes() == b""

    def test_touch_creates_parents(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        service.touch("a/b/c.txt")
        assert (workspace_root / "a" / "b" / "c.txt").is_file()

    def test_touch_preserves_existing_content(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_bytes(b"keep me")
        service.touch("a.txt")
        assert (workspace_root / "a.txt").read_bytes() == b"keep me"

    def test_touch_updates_mtime(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        import os
        import time

        f = workspace_root / "a.txt"
        f.write_bytes(b"x")
        old_mtime = f.stat().st_mtime
        os.utime(f, (old_mtime - 10, old_mtime - 10))
        time.sleep(0.01)
        service.touch("a.txt")
        assert f.stat().st_mtime > old_mtime - 10

    def test_touch_on_existing_directory_is_noop(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "docs").mkdir()
        service.touch("docs")
        assert (workspace_root / "docs").is_dir()

    def test_touch_respects_cwd(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "docs").mkdir()
        service.cd("/docs")
        service.touch("note.md")
        assert (workspace_root / "docs" / "note.md").is_file()


class TestEditText:
    """Find-and-replace редактирование файла."""

    def test_replace_unique_occurrence(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("hello world")
        applied = service.edit_text("a.txt", "world", "there")
        assert applied == 1
        assert (workspace_root / "a.txt").read_text() == "hello there"

    def test_replace_empty_new_deletes_substring(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("hello world")
        applied = service.edit_text("a.txt", " world", "")
        assert applied == 1
        assert (workspace_root / "a.txt").read_text() == "hello"

    def test_missing_substring_raises_error(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("hello")
        with pytest.raises(WorkspaceError) as exc:
            service.edit_text("a.txt", "world", "there")
        assert not isinstance(exc.value, WorkspaceNotFoundError)
        assert "not found" in str(exc.value)
        assert (workspace_root / "a.txt").read_text() == "hello"

    def test_ambiguous_without_replace_all_raises_error(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("foo foo foo")
        with pytest.raises(WorkspaceError) as exc:
            service.edit_text("a.txt", "foo", "bar")
        assert "matches 3 times" in str(exc.value)
        assert (workspace_root / "a.txt").read_text() == "foo foo foo"

    def test_replace_all_replaces_every_occurrence(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("foo foo foo")
        applied = service.edit_text("a.txt", "foo", "bar", replace_all=True)
        assert applied == 3
        assert (workspace_root / "a.txt").read_text() == "bar bar bar"

    def test_missing_file_raises_not_found(
        self, service: FsProjectWorkspaceShell
    ) -> None:
        with pytest.raises(WorkspaceNotFoundError) as exc:
            service.edit_text("ghost.txt", "a", "b")
        assert exc.value.path == "ghost.txt"

    def test_empty_old_string_raises(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("x")
        with pytest.raises(WorkspaceError) as exc:
            service.edit_text("a.txt", "", "y")
        assert "must not be empty" in str(exc.value)

    def test_cwd_relative_path(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "docs").mkdir()
        (workspace_root / "docs" / "a.txt").write_text("hi")
        service.cd("/docs")
        service.edit_text("a.txt", "hi", "hello")
        assert (workspace_root / "docs" / "a.txt").read_text() == "hello"

    def test_multiline_substring(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("line1\nline2\nline3\n")
        applied = service.edit_text("a.txt", "line2\nline3", "LINE")
        assert applied == 1
        assert (workspace_root / "a.txt").read_text() == "line1\nLINE\n"


class TestGrep:
    """Поиск по содержимому файлов."""

    def test_simple_match_single_file(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("foo\nbar\nbaz\n")
        matches = list(service.grep("bar"))
        assert len(matches) == 1
        assert matches[0].path == "a.txt"
        assert matches[0].line == 2
        assert matches[0].content == "bar"

    def test_recursive_across_tree(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("alpha\n")
        (workspace_root / "docs").mkdir()
        (workspace_root / "docs" / "b.txt").write_text("alpha here too\n")
        matches = list(service.grep("alpha"))
        paths = sorted(m.path for m in matches)
        assert paths == ["a.txt", "docs/b.txt"]

    def test_include_glob_filters(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.py").write_text("import x\n")
        (workspace_root / "b.md").write_text("import x\n")
        matches = list(service.grep("import", include="*.py"))
        assert [m.path for m in matches] == ["a.py"]

    def test_regex_pattern(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("v1.2.3\nv10.0.0\nhello\n")
        matches = list(service.grep(r"^v\d+\.\d+\.\d+$"))
        assert [m.line for m in matches] == [1, 2]

    def test_fixed_string_escapes_regex_meta(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("config.json\nconfigXjson\n")
        matches = list(service.grep("config.json", fixed_string=True))
        assert [m.line for m in matches] == [1]

    def test_invalid_regex_raises_error(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("x")
        with pytest.raises(WorkspaceError) as exc:
            list(service.grep("[unclosed"))
        assert not isinstance(exc.value, WorkspaceNotFoundError)
        assert "invalid regex" in str(exc.value)

    def test_case_insensitive(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("Hello\nHELLO\n")
        matches = list(service.grep("hello", case_insensitive=True))
        assert len(matches) == 2

    def test_context_lines(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("l1\nl2\nl3\nMATCH\nl5\nl6\nl7\n")
        matches = list(service.grep("MATCH", context=2))
        assert len(matches) == 1
        m = matches[0]
        assert m.before == ("l2", "l3")
        assert m.after == ("l5", "l6")

    def test_binary_file_skipped(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("hello\n")
        (workspace_root / "b.bin").write_bytes(b"hello\x00\x01\x02")
        matches = list(service.grep("hello"))
        assert [m.path for m in matches] == ["a.txt"]

    def test_missing_path_raises_not_found(
        self, service: FsProjectWorkspaceShell
    ) -> None:
        with pytest.raises(WorkspaceNotFoundError):
            list(service.grep("x", "nowhere"))

    def test_limit_truncates(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "a.txt").write_text("x\n" * 10)
        matches = list(service.grep("x", limit=3))
        assert len(matches) == 3

    def test_path_uses_cwd_when_none(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        (workspace_root / "docs").mkdir()
        (workspace_root / "docs" / "a.txt").write_text("inside\n")
        (workspace_root / "other.txt").write_text("outside\n")
        service.cd("/docs")
        matches = list(service.grep("inside"))
        assert [m.path for m in matches] == ["docs/a.txt"]
        matches_out = list(service.grep("outside"))
        assert matches_out == []


class TestServiceHappyPath:
    """Реальные операции чтения/записи через нормализованные пути."""

    def test_write_then_read_same_file_by_absolute_and_relative(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        with service.write_text("/note.txt") as f:
            f.write("hi")
        with service.read_text("./note.txt") as f:
            assert f.read() == "hi"
        assert (workspace_root / "note.txt").read_text() == "hi"

    def test_escape_and_clean_reach_same_file(
        self, service: FsProjectWorkspaceShell, workspace_root: Path
    ) -> None:
        with service.write_text("a/b/c.txt") as f:
            f.write("x")
        with service.read_text("../../a/b/c.txt") as f:
            assert f.read() == "x"
