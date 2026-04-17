"""Тесты JsonLinesHistoryService поверх файлового workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

from boba.adapters.fs_workspace import FsWorkspaceService
from boba.adapters.jsonl_history import JsonLinesHistoryService
from boba.domain.agent.events import (
    AnswerComplete,
    AnswerToken,
    UserQueryReceived,
)
from boba.domain.agent.models import RequestId
from boba.domain.core.workspace import WorkspaceId

HISTORY_FILE = "history.jsonl"


@pytest.fixture
def ws(tmp_path: Path) -> FsWorkspaceService:
    return FsWorkspaceService(WorkspaceId.new(), tmp_path)


@pytest.fixture
def rid() -> RequestId:
    return RequestId.new()


def _append_raw(ws: FsWorkspaceService, text: str) -> None:
    """Дописать произвольный текст в файл истории (минуя сервис)."""
    with ws.append_text(HISTORY_FILE) as f:
        f.write(text)


class TestAppendAndEntries:
    def test_append_returns_entry_with_event(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        svc = JsonLinesHistoryService(ws)
        entry = svc.append(UserQueryReceived(request_id=rid, query="hi"))

        assert entry.parent_id is None
        assert entry.request_id == rid
        assert isinstance(entry.event, UserQueryReceived)
        assert entry.event.query == "hi"

    def test_parent_id_chain(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        svc = JsonLinesHistoryService(ws)
        e1 = svc.append(UserQueryReceived(request_id=rid, query="q"))
        e2 = svc.append(AnswerComplete(request_id=rid, content="a"))
        e3 = svc.append(AnswerToken(request_id=rid, token="t"))

        assert e1.parent_id is None
        assert e2.parent_id == e1.id
        assert e3.parent_id == e2.id

    def test_entries_forward_roundtrip(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        svc = JsonLinesHistoryService(ws)
        e1 = svc.append(UserQueryReceived(request_id=rid, query="q"))
        e2 = svc.append(AnswerComplete(request_id=rid, content="answer"))
        e3 = svc.append(AnswerToken(request_id=rid, token="tok"))

        read = list(svc.entries())

        assert [e.id for e in read] == [e1.id, e2.id, e3.id]
        assert [e.parent_id for e in read] == [None, e1.id, e2.id]
        assert isinstance(read[0].event, UserQueryReceived)
        assert read[0].event.query == "q"
        assert isinstance(read[1].event, AnswerComplete)
        assert read[1].event.content == "answer"
        assert isinstance(read[2].event, AnswerToken)
        assert read[2].event.token == "tok"


class TestReverseReading:
    def test_entries_reverse_order(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        svc = JsonLinesHistoryService(ws)
        e1 = svc.append(UserQueryReceived(request_id=rid, query="q"))
        e2 = svc.append(AnswerComplete(request_id=rid, content="a"))
        e3 = svc.append(AnswerToken(request_id=rid, token="t"))

        read = list(svc.entries(reverse=True))

        assert [e.id for e in read] == [e3.id, e2.id, e1.id]

    def test_entries_reverse_on_empty_file(
        self, ws: FsWorkspaceService
    ) -> None:
        svc = JsonLinesHistoryService(ws)
        assert list(svc.entries(reverse=True)) == []

    def test_entries_reverse_single_entry(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        svc = JsonLinesHistoryService(ws)
        e1 = svc.append(UserQueryReceived(request_id=rid, query="only"))
        read = list(svc.entries(reverse=True))
        assert [e.id for e in read] == [e1.id]


class TestRecoveryLastId:
    def test_reopen_continues_parent_chain(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        s1 = JsonLinesHistoryService(ws)
        _ = s1.append(UserQueryReceived(request_id=rid, query="q"))
        e2 = s1.append(AnswerComplete(request_id=rid, content="a"))

        s2 = JsonLinesHistoryService(ws)
        e3 = s2.append(AnswerToken(request_id=rid, token="t"))

        assert e3.parent_id == e2.id

    def test_recovery_on_empty_file(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        svc = JsonLinesHistoryService(ws)
        e = svc.append(UserQueryReceived(request_id=rid, query="first"))
        assert e.parent_id is None


class TestPartialAndMalformedLines:
    def test_partial_tail_skipped_in_entries_forward(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        svc = JsonLinesHistoryService(ws)
        e1 = svc.append(UserQueryReceived(request_id=rid, query="q"))
        e2 = svc.append(AnswerComplete(request_id=rid, content="a"))

        _append_raw(ws, '{"id": "abc", "parent_id": null, "request_')

        read = list(svc.entries())
        assert [e.id for e in read] == [e1.id, e2.id]

    def test_partial_tail_skipped_in_entries_reverse(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        svc = JsonLinesHistoryService(ws)
        e1 = svc.append(UserQueryReceived(request_id=rid, query="q"))
        e2 = svc.append(AnswerComplete(request_id=rid, content="a"))

        _append_raw(ws, '{"partially written')

        read = list(svc.entries(reverse=True))
        assert [e.id for e in read] == [e2.id, e1.id]

    def test_recovery_ignores_partial_tail(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        s1 = JsonLinesHistoryService(ws)
        _ = s1.append(UserQueryReceived(request_id=rid, query="q"))
        e2 = s1.append(AnswerComplete(request_id=rid, content="a"))

        _append_raw(ws, '{"broken partial')

        s2 = JsonLinesHistoryService(ws)
        e3 = s2.append(AnswerToken(request_id=rid, token="t"))

        assert e3.parent_id == e2.id

    def test_malformed_middle_line_skipped_forward(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        svc = JsonLinesHistoryService(ws)
        e1 = svc.append(UserQueryReceived(request_id=rid, query="q"))
        _append_raw(ws, "not a json line at all\n")
        e2 = svc.append(AnswerComplete(request_id=rid, content="a"))

        read = list(svc.entries())
        assert [e.id for e in read] == [e1.id, e2.id]

    def test_malformed_middle_line_skipped_reverse(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        svc = JsonLinesHistoryService(ws)
        e1 = svc.append(UserQueryReceived(request_id=rid, query="q"))
        _append_raw(ws, "garbage\n")
        e2 = svc.append(AnswerComplete(request_id=rid, content="a"))

        read = list(svc.entries(reverse=True))
        assert [e.id for e in read] == [e2.id, e1.id]

    def test_unknown_event_type_skipped(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        svc = JsonLinesHistoryService(ws)
        e1 = svc.append(UserQueryReceived(request_id=rid, query="q"))
        _append_raw(
            ws,
            '{"id": "00000000-0000-0000-0000-000000000001", '
            '"parent_id": null, '
            '"request_id": "00000000-0000-0000-0000-000000000002", '
            '"timestamp": "2025-01-01T00:00:00+00:00", '
            '"event_type": "DefinitelyNotAnEvent", '
            '"event": {}}\n',
        )
        e2 = svc.append(AnswerComplete(request_id=rid, content="a"))

        read = list(svc.entries())
        assert [e.id for e in read] == [e1.id, e2.id]

    def test_blank_lines_skipped(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        svc = JsonLinesHistoryService(ws)
        e1 = svc.append(UserQueryReceived(request_id=rid, query="q"))
        _append_raw(ws, "\n\n")
        e2 = svc.append(AnswerComplete(request_id=rid, content="a"))

        read = list(svc.entries())
        assert [e.id for e in read] == [e1.id, e2.id]
