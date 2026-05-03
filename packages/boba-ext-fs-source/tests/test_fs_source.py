"""FsSource: walk, content_hint по расширению, фильтры include/exclude."""

from __future__ import annotations

from pathlib import Path

from boba.ext.fs_source.config import FsSourceConfig
from boba.ext.fs_source.source import FsSource
from boba.indexing import IndexingContext, PipelineId, SourceId


def _ctx() -> IndexingContext:
    return IndexingContext(pipeline_id=PipelineId("test"), collection="test")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_walks_directory_recursively(tmp_path: Path):
    _write(tmp_path / "a.md", "alpha")
    _write(tmp_path / "sub" / "b.txt", "beta")
    src = FsSource(FsSourceConfig(paths=[str(tmp_path)]))

    items = list(src.stream(_ctx()))

    sids = {i.source_id for i in items}
    assert sids == {
        f"fs:{(tmp_path / 'a.md').resolve()}",
        f"fs:{(tmp_path / 'sub' / 'b.txt').resolve()}",
    }
    by_hint = {i.content_hint for i in items}
    assert by_hint == {"md", "txt"}


def test_skips_hidden_segments(tmp_path: Path):
    _write(tmp_path / "visible.md", "x")
    _write(tmp_path / ".hidden" / "ignored.md", "y")
    _write(tmp_path / "sub" / ".secret.md", "z")
    src = FsSource(FsSourceConfig(paths=[str(tmp_path)]))

    sids = {i.source_id for i in src.stream(_ctx())}

    assert sids == {f"fs:{(tmp_path / 'visible.md').resolve()}"}


def test_include_filter(tmp_path: Path):
    _write(tmp_path / "a.md", "x")
    _write(tmp_path / "b.txt", "y")
    _write(tmp_path / "c.html", "z")
    src = FsSource(
        FsSourceConfig(paths=[str(tmp_path)], include=["*.md", "*.html"])
    )

    hints = sorted(i.content_hint for i in src.stream(_ctx()))

    assert hints == ["html", "md"]


def test_exclude_filter(tmp_path: Path):
    _write(tmp_path / "a.md", "x")
    _write(tmp_path / "drafts" / "d.md", "draft")
    src = FsSource(FsSourceConfig(paths=[str(tmp_path)], exclude=["d.md"]))

    sids = {i.source_id for i in src.stream(_ctx())}

    assert sids == {f"fs:{(tmp_path / 'a.md').resolve()}"}


def test_single_file_path(tmp_path: Path):
    target = tmp_path / "only.txt"
    _write(target, "hello")
    src = FsSource(FsSourceConfig(paths=[str(target)]))

    items = list(src.stream(_ctx()))

    assert len(items) == 1
    assert items[0].payload == b"hello"


def test_factory_id():
    src = FsSource(FsSourceConfig(paths=[]))
    assert src.source_factory_id() == SourceId("ext.fs")
