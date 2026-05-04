"""FsTransport + FsWalkRequestSource: walk + open + identity + metadata."""

from __future__ import annotations

from pathlib import Path

import pytest

from boba.fs_transport import FsRequest, FsTransport, FsWalkRequestSource
from boba.indexing import IndexingContext, PipelineId


def _ctx() -> IndexingContext:
    return IndexingContext(pipeline_id=PipelineId("t"), collection="c")


def test_fs_transport_opens_file_and_propagates_metadata(tmp_path: Path):
    """Handle живёт ровно один шаг generator'а; Reader читает в том же цикле."""
    f = tmp_path / "x.txt"
    f.write_text("hello\nworld\n")
    req = FsRequest(
        path=str(f),
        source_id="fs:custom-id",
        metadata={"my_field": "v"},
    )
    seen = []
    for doc in FsTransport().stream(_ctx(), iter([req])):
        seen.append(
            (
                doc.source_id,
                dict(doc.metadata),
                doc.handle.read(),  # читаем ВНУТРИ цикла, до закрытия
            )
        )
    assert len(seen) == 1
    sid, md, payload = seen[0]
    assert sid == "fs:custom-id"
    assert md["my_field"] == "v"
    assert md["mtime"]
    assert md["size"] == str(f.stat().st_size)
    assert payload == b"hello\nworld\n"


def test_fs_transport_missing_source_id_raises(tmp_path: Path):
    """Transport — исполнитель: identity обязан быть установлен RequestSource'ом."""
    f = tmp_path / "y.md"
    f.write_text("# md")
    with pytest.raises(ValueError, match="source_id"):
        list(FsTransport().stream(_ctx(), iter([FsRequest(path=str(f))])))


def test_fs_transport_skips_missing_file(tmp_path: Path):
    """missing-файл не генерит RawDocument; source_id обязателен (как контракт)."""
    seen = list(
        FsTransport().stream(
            _ctx(),
            iter(
                [
                    FsRequest(
                        path=str(tmp_path / "no-such-file"),
                        source_id="fs:/no-such-file",
                    )
                ]
            ),
        )
    )
    assert seen == []


def test_walk_request_source_yields_files(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.md").write_text("b")
    (tmp_path / ".hidden").write_text("h")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.html").write_text("<p>c</p>")
    src = FsWalkRequestSource(paths=[str(tmp_path)])
    requests = list(src.stream(_ctx()))
    paths = sorted(r.path for r in requests)
    # .hidden пропущен
    assert any("a.txt" in p for p in paths)
    assert any("b.md" in p for p in paths)
    assert any("c.html" in p for p in paths)
    assert not any(".hidden" in p for p in paths)


def test_walk_include_filter(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.md").write_text("b")
    src = FsWalkRequestSource(paths=[str(tmp_path)], include=("*.md",))
    requests = list(src.stream(_ctx()))
    assert len(requests) == 1
    assert requests[0].path.endswith(".md")


def test_walk_exclude_filter(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.md").write_text("b")
    src = FsWalkRequestSource(paths=[str(tmp_path)], exclude=("*.md",))
    requests = list(src.stream(_ctx()))
    assert len(requests) == 1
    assert requests[0].path.endswith(".txt")


def test_walk_list_source_ids(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.md").write_text("b")
    src = FsWalkRequestSource(paths=[str(tmp_path)])
    ids = sorted(src.list_source_ids(_ctx()))
    assert all(s.startswith("fs:") for s in ids)
    assert len(ids) == 2


def test_walk_request_source_to_transport_pipeline(tmp_path: Path):
    (tmp_path / "a.txt").write_text("ALPHA")
    src = FsWalkRequestSource(paths=[str(tmp_path)])
    transport = FsTransport()
    payloads = []
    for doc in transport.stream(_ctx(), src.stream(_ctx())):
        payloads.append(doc.handle.read())  # читаем в цикле, не в list-comprehension
    assert payloads == [b"ALPHA"]
