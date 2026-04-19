"""Тесты JsonLinesHistoryService поверх файлового workspace."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from boba.adapters.fs_workspace import FsWorkspaceService
from boba.adapters.jsonl_history import (
    HistoryEntryDecoder,
    HistoryEntryEncoder,
    JsonLinesHistoryService,
)
from boba.domain.agent.events import (
    AgentEvent,
    AnswerComplete,
    AnswerStarted,
    AnswerToken,
    GenerationDone,
    GenerationStarted,
    RefusalComplete,
    RefusalToken,
    StageCompleted,
    StageStarted,
    ThinkingComplete,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
    ToolCallComplete,
    ToolExecutionFailed,
    ToolResultReady,
    UserQueryReceived,
)
from boba.domain.agent.models import RequestId
from boba.domain.core.history import EntryId, HistoryEntry, HistoryReadError
from boba.domain.core.patterns import ConverterInputError
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

    def test_parent_id_chain(self, ws: FsWorkspaceService, rid: RequestId) -> None:
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

    def test_entries_reverse_on_empty_file(self, ws: FsWorkspaceService) -> None:
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
    def test_partial_tail_raises_in_entries_forward(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        svc = JsonLinesHistoryService(ws)
        _ = svc.append(UserQueryReceived(request_id=rid, query="q"))
        _ = svc.append(AnswerComplete(request_id=rid, content="a"))

        _append_raw(ws, '{"id": "abc", "parent_id": null, "request_')

        with pytest.raises(HistoryReadError):
            list(svc.entries())

    def test_partial_tail_raises_in_entries_reverse(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        svc = JsonLinesHistoryService(ws)
        _ = svc.append(UserQueryReceived(request_id=rid, query="q"))
        _ = svc.append(AnswerComplete(request_id=rid, content="a"))

        _append_raw(ws, '{"partially written')

        with pytest.raises(HistoryReadError):
            list(svc.entries(reverse=True))

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

    def test_malformed_middle_line_raises_forward(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        svc = JsonLinesHistoryService(ws)
        _ = svc.append(UserQueryReceived(request_id=rid, query="q"))
        _append_raw(ws, "not a json line at all\n")
        _ = svc.append(AnswerComplete(request_id=rid, content="a"))

        with pytest.raises(HistoryReadError):
            list(svc.entries())

    def test_malformed_middle_line_raises_reverse(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        svc = JsonLinesHistoryService(ws)
        _ = svc.append(UserQueryReceived(request_id=rid, query="q"))
        _append_raw(ws, "garbage\n")
        _ = svc.append(AnswerComplete(request_id=rid, content="a"))

        with pytest.raises(HistoryReadError):
            list(svc.entries(reverse=True))

    def test_unknown_event_type_raises(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        svc = JsonLinesHistoryService(ws)
        _ = svc.append(UserQueryReceived(request_id=rid, query="q"))
        _append_raw(
            ws,
            '{"id": "00000000-0000-0000-0000-000000000001", '
            '"parent_id": null, '
            '"request_id": "00000000-0000-0000-0000-000000000002", '
            '"timestamp": "2025-01-01T00:00:00+00:00", '
            '"event_type": "DefinitelyNotAnEvent", '
            '"event": {}}\n',
        )
        _ = svc.append(AnswerComplete(request_id=rid, content="a"))

        with pytest.raises(HistoryReadError):
            list(svc.entries())

    def test_blank_lines_skipped(self, ws: FsWorkspaceService, rid: RequestId) -> None:
        svc = JsonLinesHistoryService(ws)
        e1 = svc.append(UserQueryReceived(request_id=rid, query="q"))
        _append_raw(ws, "\n\n")
        e2 = svc.append(AnswerComplete(request_id=rid, content="a"))

        read = list(svc.entries())
        assert [e.id for e in read] == [e1.id, e2.id]

    def test_truncated_malformed_middle_line_raises(
        self, ws: FsWorkspaceService, rid: RequestId
    ) -> None:
        svc = JsonLinesHistoryService(ws)
        _ = svc.append(UserQueryReceived(request_id=rid, query="q"))
        _append_raw(
            ws,
            '{"id": "00000000-0000-0000-0000-000000000001", '
            '"parent_id": null, "request_id": "00000000-0000-0000-\n',
        )
        _ = svc.append(AnswerComplete(request_id=rid, content="a"))

        with pytest.raises(HistoryReadError) as exc_info:
            list(svc.entries())
        assert isinstance(exc_info.value.__cause__, ConverterInputError)

        with pytest.raises(HistoryReadError):
            list(svc.entries(reverse=True))


# ---------------------------------------------------------------------------
# Converter tests: HistoryEntryEncoder / HistoryEntryDecoder без workspace.
# ---------------------------------------------------------------------------


EventFactory = Callable[[RequestId], AgentEvent]

_EVENT_FACTORIES: list[tuple[str, EventFactory]] = [
    (
        "UserQueryReceived",
        lambda rid: UserQueryReceived(request_id=rid, query="привет мир"),
    ),
    (
        "StageStarted",
        lambda rid: StageStarted(request_id=rid, stage="init"),
    ),
    (
        "StageCompleted",
        lambda rid: StageCompleted(request_id=rid, stage="init", detail="done"),
    ),
    ("GenerationStarted", lambda rid: GenerationStarted(request_id=rid)),
    ("ThinkingStarted", lambda rid: ThinkingStarted(request_id=rid)),
    (
        "ThinkingToken",
        lambda rid: ThinkingToken(request_id=rid, token="hmm "),
    ),
    (
        "ThinkingComplete",
        lambda rid: ThinkingComplete(request_id=rid, content="full thought"),
    ),
    ("AnswerStarted", lambda rid: AnswerStarted(request_id=rid)),
    (
        "AnswerToken",
        lambda rid: AnswerToken(request_id=rid, token="hello "),
    ),
    (
        "AnswerComplete",
        lambda rid: AnswerComplete(request_id=rid, content="full answer"),
    ),
    (
        "RefusalToken",
        lambda rid: RefusalToken(request_id=rid, token="no "),
    ),
    (
        "RefusalComplete",
        lambda rid: RefusalComplete(request_id=rid, content="cannot"),
    ),
    (
        "GenerationDone",
        lambda rid: GenerationDone(request_id=rid, finish_reason="stop"),
    ),
    (
        "ToolCallBegin",
        lambda rid: ToolCallBegin(
            request_id=rid, index=0, tool_call_id="tc1", tool_name="search"
        ),
    ),
    (
        "ToolCallArgumentDelta",
        lambda rid: ToolCallArgumentDelta(request_id=rid, index=0, arguments='{"q":'),
    ),
    (
        "ToolCallComplete",
        lambda rid: ToolCallComplete(
            request_id=rid,
            tool_call_id="tc1",
            tool_name="search",
            arguments='{"q":"x"}',
        ),
    ),
    (
        "ToolResultReady",
        lambda rid: ToolResultReady(
            request_id=rid,
            tool_call_id="tc1",
            tool_name="search",
            content="result",
        ),
    ),
    (
        "ToolExecutionFailed",
        lambda rid: ToolExecutionFailed(
            request_id=rid,
            tool_call_id="tc1",
            tool_name="search",
            error_kind="ToolExecutionError",
            message="boom",
        ),
    ),
]


def _make_entry(
    event: AgentEvent,
    rid: RequestId,
    *,
    parent: EntryId | None = None,
) -> HistoryEntry:
    return HistoryEntry(
        id=EntryId.new(),
        parent_id=parent,
        request_id=rid,
        timestamp=datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC),
        event=event,
    )


class TestConverterRoundTrip:
    """encoder.convert → decoder.convert восстанавливает исходный HistoryEntry."""

    @pytest.mark.parametrize(
        ("label", "factory"),
        _EVENT_FACTORIES,
        ids=[label for label, _ in _EVENT_FACTORIES],
    )
    def test_roundtrip(self, rid: RequestId, label: str, factory: EventFactory) -> None:
        del label
        event = factory(rid)
        entry = _make_entry(event, rid, parent=EntryId.new())

        line = HistoryEntryEncoder().convert(entry)
        restored = HistoryEntryDecoder().convert(line)

        assert restored == entry

    def test_roundtrip_without_parent(self, rid: RequestId) -> None:
        entry = _make_entry(
            UserQueryReceived(request_id=rid, query="hi"), rid, parent=None
        )

        line = HistoryEntryEncoder().convert(entry)
        restored = HistoryEntryDecoder().convert(line)

        assert restored == entry
        assert restored.parent_id is None


class TestEncoderShape:
    """Проверяем форму JSON на выходе кодера."""

    def test_top_level_keys(self, rid: RequestId) -> None:
        entry = _make_entry(UserQueryReceived(request_id=rid, query="hi"), rid)
        parsed = json.loads(HistoryEntryEncoder().convert(entry))

        assert set(parsed.keys()) == {
            "id",
            "parent_id",
            "request_id",
            "timestamp",
            "event_type",
            "event",
        }

    def test_event_type_is_class_name(self, rid: RequestId) -> None:
        entry = _make_entry(AnswerComplete(request_id=rid, content="x"), rid)
        parsed = json.loads(HistoryEntryEncoder().convert(entry))
        assert parsed["event_type"] == "AnswerComplete"

    def test_event_is_flat_dict(self, rid: RequestId) -> None:
        entry = _make_entry(
            ToolCallBegin(request_id=rid, index=0, tool_call_id="tc", tool_name="fn"),
            rid,
        )
        parsed = json.loads(HistoryEntryEncoder().convert(entry))

        assert isinstance(parsed["event"], dict)
        assert set(parsed["event"].keys()) == {
            "request_id",
            "index",
            "tool_call_id",
            "tool_name",
        }
        assert "event_data" not in parsed
        assert "event_data" not in parsed["event"]

    def test_parent_id_null_when_none(self, rid: RequestId) -> None:
        entry = _make_entry(
            UserQueryReceived(request_id=rid, query="hi"), rid, parent=None
        )
        parsed = json.loads(HistoryEntryEncoder().convert(entry))
        assert parsed["parent_id"] is None

    def test_ids_are_uuid_strings(self, rid: RequestId) -> None:
        entry = _make_entry(
            UserQueryReceived(request_id=rid, query="hi"),
            rid,
            parent=EntryId.new(),
        )
        parsed = json.loads(HistoryEntryEncoder().convert(entry))

        assert parsed["id"] == str(entry.id.name)
        assert parsed["parent_id"] == str(entry.parent_id.name)  # type: ignore[union-attr]
        assert parsed["request_id"] == str(rid.name)
        assert parsed["event"]["request_id"] == str(rid.name)

    def test_timestamp_is_iso(self, rid: RequestId) -> None:
        entry = _make_entry(UserQueryReceived(request_id=rid, query="hi"), rid)
        parsed = json.loads(HistoryEntryEncoder().convert(entry))
        assert parsed["timestamp"] == "2025-01-02T03:04:05+00:00"

    def test_unicode_preserved(self, rid: RequestId) -> None:
        entry = _make_entry(
            UserQueryReceived(request_id=rid, query="кириллица 🎉"), rid
        )
        line = HistoryEntryEncoder().convert(entry)
        # ensure_ascii=False → символы НЕ экранируются в \uXXXX
        assert "кириллица" in line
        assert "🎉" in line


class TestDecoderErrors:
    """Проверяем ошибки, которые должен возвращать декодер."""

    @staticmethod
    def _valid_base_line(rid: RequestId) -> dict[str, object]:
        entry = _make_entry(UserQueryReceived(request_id=rid, query="hi"), rid)
        return json.loads(HistoryEntryEncoder().convert(entry))

    def test_unknown_event_type_raises(self, rid: RequestId) -> None:
        payload = self._valid_base_line(rid)
        payload["event_type"] = "BogusEvent"
        line = json.dumps(payload)

        with pytest.raises(ConverterInputError, match="unknown event_type"):
            HistoryEntryDecoder().convert(line)

    def test_missing_top_level_field_raises(self, rid: RequestId) -> None:
        payload = self._valid_base_line(rid)
        del payload["request_id"]
        line = json.dumps(payload)

        with pytest.raises(ConverterInputError, match="missing required field"):
            HistoryEntryDecoder().convert(line)

    def test_missing_event_payload_field_raises(self, rid: RequestId) -> None:
        payload = self._valid_base_line(rid)
        # UserQueryReceived требует поле "query"
        payload["event"] = {"request_id": str(rid.name)}
        line = json.dumps(payload)

        with pytest.raises(ConverterInputError, match="missing required field"):
            HistoryEntryDecoder().convert(line)

    def test_malformed_json_raises(self) -> None:
        with pytest.raises(ConverterInputError, match="malformed JSON"):
            HistoryEntryDecoder().convert('{"broken')

    def test_invalid_uuid_in_id_raises(self, rid: RequestId) -> None:
        payload = self._valid_base_line(rid)
        payload["id"] = "not-a-uuid"
        line = json.dumps(payload)

        with pytest.raises(ConverterInputError, match="badly formed"):
            HistoryEntryDecoder().convert(line)

    def test_raw_exception_preserved_as_cause(self, rid: RequestId) -> None:
        payload = self._valid_base_line(rid)
        del payload["request_id"]
        line = json.dumps(payload)

        with pytest.raises(ConverterInputError) as exc_info:
            HistoryEntryDecoder().convert(line)

        assert isinstance(exc_info.value.__cause__, KeyError)
