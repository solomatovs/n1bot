"""Tools stream_logs: отчёт о занятости тома и уборка журналов треда.

Журнал настоящий: файлы пишет writer-поток песочницы, инструмент их считает и
сносит — как в приложении, только вместо стадии в канал пишет тест. Ключевые
инварианты: текущий и живой треды не сносятся, чужой сегмент пути до тома не
доезжает, а отчёт называет тред, который потом принимает уборка.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from chainlit.context import init_http_context
from chainlit.user import PersistedUser
from langchain_core.messages import ToolMessage

from boba.chainlit.agent.tools.stream_logs import (
    StreamLogsErrorKind,
    StreamLogsOps,
    build_stream_logs_tools,
)
from boba.sandbox.journal import (
    CallJournal,
    DirVault,
    StreamJournal,
    StreamJournalHub,
)
from boba.sandbox.runner import ToolCallContext
from boba.toolkit.channels import Channel, StreamFormat
from boba.toolkit.result import ErrorResult, TextResult, ToolResult

USER = "7"
THREAD = "th-current"
OTHER_THREAD = "th-other"
BODY = b"a,b\n1,2\n" * 64


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Сессию заводит сам тест: она нужна внутри его event loop."""


@pytest.fixture(autouse=True)
def journal(tmp_path: Path) -> Iterator[StreamJournal]:
    """Журнал приложения в каталоге теста: хаб глобален и возвращается назад."""
    made = StreamJournal(DirVault(str(tmp_path / "vault")), reserve_bytes=0)
    StreamJournalHub.configure(made)

    yield made

    StreamJournalHub.reset()


def opened(journal: StreamJournal, thread_id: str, call_id: str) -> CallJournal:
    context = ToolCallContext(
        user_id=USER, thread_id=thread_id, call_id=call_id, tool="bash"
    )

    return journal.open(context)


def recorded(journal: StreamJournal, thread_id: str, call_id: str) -> None:
    """Завершённый вызов: файлы дописаны, writer-поток остановлен."""
    call = opened(journal, thread_id, call_id)
    sink = call.sink("bash", Channel.TOOL_PAYLOAD, StreamFormat.CSV)
    sink.feed(BODY)
    sink.close()
    call.close("")


def in_session(action: Any) -> Any:
    """Действие внутри сессии chainlit: контекст живёт в её event loop."""

    async def run() -> Any:
        user = PersistedUser(
            id=USER, identifier="tester", createdAt="2026-01-01T00:00:00Z"
        )
        init_http_context(user=user, thread_id=THREAD)

        return action()

    return asyncio.run(run())


def error_of(result: ToolResult) -> ErrorResult:
    assert isinstance(result, ErrorResult)

    return result


def text_of(result: ToolResult) -> str:
    assert isinstance(result, TextResult)

    return result.text


class TestUsageReport:
    """Отчёт называет том и треды, старые первыми, с пометкой текущего."""

    def test_threads_are_listed_oldest_first(self, journal: StreamJournal) -> None:
        recorded(journal, OTHER_THREAD, "c-old")
        recorded(journal, THREAD, "c-new")

        report = text_of(in_session(StreamLogsOps.usage))

        assert "volume:" in report
        assert report.index(OTHER_THREAD) < report.index(THREAD)
        assert "(current thread, cannot be purged)" in report
        assert f"- {OTHER_THREAD}: " in report
        assert " in 1 calls" in report

    def test_an_empty_vault_is_reported_as_such(self) -> None:
        report = text_of(in_session(StreamLogsOps.usage))

        assert "journals: none" in report

    def test_usage_outside_a_session_is_refused(self) -> None:
        failure = error_of(StreamLogsOps.usage())

        assert failure.error_kind == StreamLogsErrorKind.NO_SESSION

    def test_usage_without_a_journal_is_refused(self) -> None:
        StreamJournalHub.reset()

        failure = error_of(in_session(StreamLogsOps.usage))

        assert failure.error_kind == StreamLogsErrorKind.NO_JOURNAL


class TestCleanup:
    """Уборка сносит журналы треда и отказывается от текущего и живого."""

    def test_a_foreign_thread_is_purged(self, journal: StreamJournal) -> None:
        recorded(journal, OTHER_THREAD, "c-1")
        recorded(journal, THREAD, "c-2")

        answer = text_of(in_session(lambda: StreamLogsOps.cleanup(OTHER_THREAD)))

        assert f"journals of thread {OTHER_THREAD} deleted" in answer

        threads: list[str] = []
        for entry in journal.usage(USER).threads:
            threads.append(entry.thread_id)

        assert threads == [THREAD]

    def test_the_current_thread_is_refused(self, journal: StreamJournal) -> None:
        recorded(journal, THREAD, "c-1")

        failure = error_of(in_session(lambda: StreamLogsOps.cleanup(THREAD)))

        assert failure.error_kind == StreamLogsErrorKind.CURRENT_THREAD
        assert journal.usage(USER).threads[0].thread_id == THREAD

    def test_a_thread_with_a_running_tool_is_refused(
        self, journal: StreamJournal
    ) -> None:
        """Живой вызов держит файлы: снести их — обрубить пишущую стадию."""
        live = opened(journal, OTHER_THREAD, "c-live")
        live.sink("bash", Channel.TOOL_PAYLOAD, StreamFormat.CSV).feed(BODY)

        failure = error_of(in_session(lambda: StreamLogsOps.cleanup(OTHER_THREAD)))

        live.close("")

        assert failure.error_kind == StreamLogsErrorKind.LIVE_THREAD
        assert journal.usage(USER).threads[0].bytes_used > 0

    def test_a_finished_call_of_the_same_thread_is_purged(
        self, journal: StreamJournal
    ) -> None:
        """Реестр отпустил вызов: тред снова свободен для уборки."""
        recorded(journal, OTHER_THREAD, "c-done")

        answer = text_of(in_session(lambda: StreamLogsOps.cleanup(OTHER_THREAD)))

        assert "freed" in answer
        assert journal.usage(USER).threads == ()

    def test_an_unknown_thread_is_reported_as_missing(self) -> None:
        failure = error_of(in_session(lambda: StreamLogsOps.cleanup("th-nowhere")))

        assert failure.error_kind == StreamLogsErrorKind.NOT_FOUND

    def test_a_path_escape_is_refused(self, journal: StreamJournal) -> None:
        """Тред называет модель: сегмент вне алфавита до тома не доезжает."""
        recorded(journal, OTHER_THREAD, "c-1")

        failure = error_of(in_session(lambda: StreamLogsOps.cleanup("../../vault")))

        assert failure.error_kind == StreamLogsErrorKind.PURGE_FAILED
        assert journal.usage(USER).threads[0].thread_id == OTHER_THREAD

    def test_cleanup_outside_a_session_is_refused(self) -> None:
        failure = error_of(StreamLogsOps.cleanup(OTHER_THREAD))

        assert failure.error_kind == StreamLogsErrorKind.NO_SESSION


class TestTools:
    """Инструменты собираются с описаниями и отдают результат ленте."""

    NAMES: ClassVar[tuple[str, ...]] = ("stream_logs_usage", "stream_logs_cleanup")

    def test_tools_are_built_with_descriptions(self) -> None:
        tools = build_stream_logs_tools(None)

        names: list[str] = []
        for tool in tools:
            names.append(tool.name)
            assert tool.description

        assert tuple(names) == self.NAMES

    def test_usage_tool_returns_the_report(self, journal: StreamJournal) -> None:
        recorded(journal, OTHER_THREAD, "c-1")
        usage_tool = build_stream_logs_tools(None)[0]

        call = {
            "name": self.NAMES[0],
            "args": {},
            "id": "call-usage-1",
            "type": "tool_call",
        }
        message = in_session(lambda: usage_tool.invoke(call))

        assert isinstance(message, ToolMessage)
        assert OTHER_THREAD in str(message.content)
        assert isinstance(message.artifact, TextResult)
