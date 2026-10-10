"""Загрузчик на настоящем журнале JSONL: возобновление целого журнала и
журнала, оборванного в каждой точке хода; сверка цепочки с проверенной
моделью журнала — программой prompt-assembly."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from journal_script import JournalScript

from boba.agent.history import SessionId, SessionLease
from boba.agent.ids import IdMint, SystemClock
from boba.agent.loader import HistoryLoader, LoadReport
from boba.agent.records import (
    AssistantRecord,
    ChainRecord,
    PermissionMode,
    Record,
    RecordCodec,
    Synthetic,
    ToolDenialKind,
    UserRecord,
)
from boba.agent.session import Session
from boba.agent.template import AgentTemplate
from boba.history.jsonl import JsonlHistoryStore

pytestmark = pytest.mark.anyio

SESSION = SessionId(value="0f3b2c1e-1111-4222-8333-444455556666")
VERSION = "0.0.26.dev4"


@dataclass(frozen=True)
class Written:
    """Записанный журнал: путь к файлу и записи в порядке seq."""

    path: Path
    records: Sequence[Record]

    def chain_uuids(self) -> list[str]:
        uuids: list[str] = []
        for record in self.records:
            if isinstance(record, ChainRecord):
                uuids.append(record.uuid)

        return uuids

    def truncate(self, target: Path, kept: int) -> Written:
        """Копия журнала с первыми kept записями."""
        lines = self.path.read_text(encoding="utf-8").splitlines()
        target.write_text("\n".join(lines[:kept]) + "\n", encoding="utf-8")

        return Written(path=target, records=self.records[:kept])


class Resumed:
    """Загрузка журнала в свежую сессию."""

    def __init__(self, root: Path) -> None:
        self._store = JsonlHistoryStore(root)
        self._loader = HistoryLoader(
            self._store, AgentTemplate(), IdMint(), SystemClock(), VERSION
        )

    async def load(self, session_id: SessionId) -> tuple[Session, LoadReport]:
        session = Session(session_id)
        report = await self._loader.load(session)

        return session, report

    async def flush(self, session: Session, report: LoadReport) -> None:
        lease = await self._store.acquire(session.id)
        try:
            await self._store.append(lease, session.since(report.stored_seq))
        finally:
            await self._store.release(lease)


class Reference:
    """Цепочка глазами prompt-assembly: что эталонный загрузчик оставил бы."""

    def history(self, journal: Path) -> dict[str, object]:
        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "boba.prompt_assembly",
                "history",
                "--json",
                str(journal),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            msg = f"prompt-assembly history failed: {done.stderr}"
            raise AssertionError(msg)

        return json.loads(done.stdout)

    def kept_uuids(self, journal: Path) -> list[str]:
        document = self.history(journal)
        rows = document["history"]
        assert isinstance(rows, list)
        uuids: list[str] = []
        for row in rows:
            if row["line"]:
                uuids.append(row["uuid"])

        return uuids

    def appended_types(self, journal: Path) -> list[str]:
        document = self.history(journal)
        appended = document["appended"]
        assert isinstance(appended, list)

        return [row["type"] for row in appended]


@pytest.fixture
async def written(tmp_path: Path) -> Written:
    """Разговор в две пачки: ответ с двумя вызовами, результаты, ответ текстом."""
    root = tmp_path / "history"
    store = JsonlHistoryStore(root)
    lease: SessionLease = await store.acquire(SESSION)
    session = Session(SESSION)
    script = JournalScript(session, store, lease)
    try:
        await script.permission_mode(PermissionMode.DEFAULT)
        await script.enqueue("list the tables")
        await script.prompt("list the tables")
        await script.attachment("environment", "# Environment\n - Platform: linux")
        names = ["pg_tables", "ch_tables"]
        calls = await script.reply("", calls=names, thinking="I need both lists")
        await script.tool_result(calls[0], "pg: users, orders")
        await script.tool_result(calls[1], "ch: events")
        await script.reply("Two lists above.")
    finally:
        await store.release(lease)

    return Written(path=root / f"{SESSION.value}.jsonl", records=session.records())


class TestWholeJournal:
    async def test_round_trip_restores_every_record_with_seq(
        self, written: Written
    ) -> None:
        session, report = await Resumed(written.path.parent).load(SESSION)

        assert list(session.records()) == list(written.records)
        assert report.repaired == []
        assert report.skipped_lines == 0
        assert report.stored_seq == len(written.records)

    async def test_reference_loader_keeps_the_same_chain(
        self, written: Written
    ) -> None:
        reference = Reference()

        assert reference.kept_uuids(written.path) == written.chain_uuids()
        assert reference.appended_types(written.path) == []


class TestTruncatedJournal:
    """Обрывы: после user, после вложения, после ответа с двумя вызовами,
    после одного из двух результатов. Индексы — номера записей в журнале:
    1 режим, 2 очередь, 3 user, 4 вложение, 5-7 ответ (thinking, два
    tool_use), 8-9 результаты, 10 ответ текстом."""

    @pytest.mark.parametrize(
        ("kept", "expected"),
        [
            (3, ["assistant"]),
            (4, ["assistant"]),
            (7, ["user", "user", "assistant"]),
            (8, ["user", "assistant"]),
        ],
        ids=["after-user", "after-attachment", "after-two-calls", "after-one-result"],
    )
    async def test_repair_matches_claude_code(
        self, written: Written, tmp_path: Path, kept: int, expected: list[str]
    ) -> None:
        cut_root = tmp_path / "cut"
        cut_root.mkdir()
        cut = written.truncate(cut_root / f"{SESSION.value}.jsonl", kept)
        resumed = Resumed(cut_root)

        session, report = await resumed.load(SESSION)

        assert [r.type.value for r in report.repaired] == expected
        assert [r.uuid for r in session.chain()[: len(cut.chain_uuids())]] == (
            cut.chain_uuids()
        )
        self._assert_chain_is_linked(session.chain())
        last = report.repaired[-1]
        assert isinstance(last, AssistantRecord)
        assert last.message.model == Synthetic.MODEL
        assert last.message.usage.is_zero()
        assert Reference().appended_types(cut.path) == expected

    async def test_stub_results_name_the_interrupted_calls(
        self, written: Written, tmp_path: Path
    ) -> None:
        cut_root = tmp_path / "cut"
        cut_root.mkdir()
        cut = written.truncate(cut_root / f"{SESSION.value}.jsonl", 8)

        session, report = await Resumed(cut_root).load(SESSION)

        stub = report.repaired[0]
        assert isinstance(stub, UserRecord)
        assert stub.tool_denial_kind is ToolDenialKind.INTERRUPTED
        results = stub.message.tool_results()
        assert len(results) == 1
        assert results[0].is_error is True
        assert results[0].text().startswith("[Tool call interrupted:")
        calls = [r for r in cut.records if isinstance(r, AssistantRecord)]
        second_call = calls[2].message.tool_uses()[0]
        assert results[0].tool_use_id == second_call.id
        assert stub.source_tool_assistant_uuid == calls[2].uuid
        assert session.records()[-1] is report.repaired[-1]

    async def test_second_resume_appends_nothing(
        self, written: Written, tmp_path: Path
    ) -> None:
        cut_root = tmp_path / "cut"
        cut_root.mkdir()
        written.truncate(cut_root / f"{SESSION.value}.jsonl", 7)
        resumed = Resumed(cut_root)
        session, report = await resumed.load(SESSION)
        await resumed.flush(session, report)

        again, second = await resumed.load(SESSION)

        assert second.repaired == []
        assert list(again.records()) == list(session.records())
        assert Reference().appended_types(cut_root / f"{SESSION.value}.jsonl") == []

    def _assert_chain_is_linked(self, chain: Sequence[ChainRecord]) -> None:
        previous: str | None = None
        for record in chain:
            assert record.parent_uuid == previous
            previous = record.uuid


class TestCompactedJournal:
    async def test_boundary_summary_and_tail_are_restored(
        self, written: Written, tmp_path: Path
    ) -> None:
        root = written.path.parent
        store = JsonlHistoryStore(root)
        lease = await store.acquire(SESSION)
        session = Session(SESSION)
        session.restore(written.records)
        script = JournalScript(session, store, lease)
        try:
            tail = [r.uuid for r in session.chain()[-3:]]
            await script.compaction("Summary: two lists were produced.", tail)
            await script.prompt("thanks")
            await script.reply("You are welcome.")
        finally:
            await store.release(lease)

        resumed, report = await Resumed(root).load(SESSION)

        assert report.repaired == []
        conversation = resumed.conversation()
        kinds = [r.type.value for r in conversation]
        assert kinds[:3] == ["system", "attachment", "user"]
        assert [r.uuid for r in conversation[3:6]] == tail
        assert kinds[6:] == ["user", "assistant"]
        moved = conversation[5]
        assert isinstance(moved, AssistantRecord)
        assert moved.message.usage.is_zero()
        reference = Reference().kept_uuids(written.path)
        assert reference == [r.uuid for r in conversation]

    async def test_boundary_alone_is_not_a_boundary(
        self, written: Written, tmp_path: Path
    ) -> None:
        root = written.path.parent
        store = JsonlHistoryStore(root)
        lease = await store.acquire(SESSION)
        session = Session(SESSION)
        session.restore(written.records)
        script = JournalScript(session, store, lease)
        try:
            await script.compaction("Summary.", [r.uuid for r in session.chain()[-1:]])
        finally:
            await store.release(lease)
        path = root / f"{SESSION.value}.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[:-2]) + "\n", encoding="utf-8")

        resumed, report = await Resumed(root).load(SESSION)

        conversation = resumed.conversation()
        assert [r.uuid for r in conversation[:-1]] == written.chain_uuids()
        assert conversation[-1].type.value == "system"
        assert report.repaired == []


class TestDamagedJournal:
    async def test_broken_last_line_is_skipped_and_sealed(
        self, written: Written
    ) -> None:
        with written.path.open("a", encoding="utf-8") as out:
            out.write('{"type": "user", "uuid": "half')

        resumed = Resumed(written.path.parent)
        session, report = await resumed.load(SESSION)

        assert report.skipped_lines == 1
        assert "line 11 skipped: not JSON" in report.notes[0]
        assert list(session.records()) == list(written.records)

        store = JsonlHistoryStore(written.path.parent)
        lease = await store.acquire(SESSION)
        try:
            script = JournalScript(session, store, lease)
            added = await script.prompt("after the damage")
        finally:
            await store.release(lease)

        again, second = await resumed.load(SESSION)
        assert added in again.records()
        assert second.skipped_lines == 1
        assert [r.type.value for r in second.repaired] == ["assistant"]

    async def test_unknown_record_type_is_skipped(self, written: Written) -> None:
        with written.path.open("a", encoding="utf-8") as out:
            out.write('{"type": "last-prompt", "leafUuid": "x", "seq": 99}\n')

        session, report = await Resumed(written.path.parent).load(SESSION)

        assert report.skipped_records == 1
        assert "last-prompt" in report.notes[0]
        assert session.last_seq == len(written.records)


class TestCodecAgainstTheFile:
    async def test_every_line_parses_back_to_the_written_record(
        self, written: Written
    ) -> None:
        codec = RecordCodec()
        lines = written.path.read_text(encoding="utf-8").splitlines()

        parsed = [codec.parse(json.loads(line)) for line in lines]

        assert parsed == list(written.records)
