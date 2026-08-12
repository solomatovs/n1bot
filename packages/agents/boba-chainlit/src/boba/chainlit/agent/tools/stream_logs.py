"""Tools stream_logs: занятость тома журналов вывода и уборка тредов.

Журналы пишутся автоматически на каждый вызов инструмента песочницы; здесь
LLM смотрит, чем занят том пользователя, и освобождает место осознанно —
вместо слепого LRU-вытеснения. Отказ операции уходит в ленту результатом;
всё, что не перечислено, упаковывает ToolErrorGuard.

Ошибки:
ErrorResult — нет сессии либо журнала, тред текущий, занят или не найден.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.chainlit.domain.session import current_thread_id, current_user_id
from boba.sandbox.journal import (
    JournalError,
    StreamJournal,
    StreamJournalHub,
    VaultUsage,
)
from boba.toolkit.result import ErrorResult, TextResult, ToolResult, pack_result

__all__ = [
    "JournalSession",
    "StreamLogsErrorKind",
    "StreamLogsOps",
    "StreamLogsPrompt",
    "StreamLogsRefusedError",
    "UsageReport",
    "build_stream_logs_tools",
]


class StreamLogsErrorKind(StrEnum):
    """Коды отказов stream_logs: уезжают в ErrorResult.error_kind."""

    NO_SESSION = "no_session"
    NO_JOURNAL = "no_journal"
    CURRENT_THREAD = "current_thread"
    LIVE_THREAD = "live_thread"
    NOT_FOUND = "thread_not_found"
    PURGE_FAILED = "purge_failed"


class StreamLogsRefusedError(Exception):
    """Операция над журналами отклонена; текст причины готов для LLM."""

    def __init__(self, kind: StreamLogsErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class StreamLogsPrompt(StrEnum):
    """Тексты описаний инструментов stream_logs."""

    USAGE = (
        "Show disk usage of the tool output journals volume: total and free "
        "space plus per-thread journal sizes, oldest first. Call it before "
        "cleanup to decide which threads to purge."
    )
    CLEANUP = (
        "Delete tool output journals of one thread and free its space. "
        "Take thread ids from stream_logs_usage. The current thread and "
        "threads with running tools cannot be purged."
    )
    THREAD_ID = "Thread id exactly as listed by stream_logs_usage."


class UsageReport:
    """Отчёт о занятости тома в тексте для LLM и пользователя."""

    def __init__(self, usage: VaultUsage, current_thread: str) -> None:
        self._usage = usage
        self._current = current_thread

    def render(self) -> str:
        lines: list[str] = []
        used = self._usage.total_bytes - self._usage.free_bytes
        lines.append(
            f"volume: {self._fmt(used)} used of "
            f"{self._fmt(self._usage.total_bytes)}, "
            f"{self._fmt(self._usage.free_bytes)} free"
        )

        if not self._usage.threads:
            lines.append("journals: none")
            return "\n".join(lines)

        lines.append("journals by thread, oldest first:")
        for entry in self._usage.threads:
            mark = ""
            if entry.thread_id == self._current:
                mark = " (current thread, cannot be purged)"
            lines.append(
                f"- {entry.thread_id}: {self._fmt(entry.bytes_used)} "
                f"in {entry.calls} calls{mark}"
            )

        return "\n".join(lines)

    @staticmethod
    def _fmt(value: int) -> str:
        if value < 1024 * 1024:
            return f"{value / 1024:.0f} KiB"
        if value < 1024 * 1024 * 1024:
            return f"{value / 1048576:.1f} MiB"
        return f"{value / 1073741824:.2f} GiB"


@dataclass(frozen=True)
class JournalSession:
    """Журнал приложения и адрес сессии, из которой пришёл вызов."""

    journal: StreamJournal
    user_id: str
    thread_id: str


class StreamLogsOps:
    """Операции над томом журналов: отчёт о занятости и уборка треда.

    Отказ операции — результат для ленты, а не исключение: LLM читает причину
    и решает сама.
    """

    @classmethod
    def usage(cls) -> ToolResult:
        try:
            session = cls.session()
            usage = session.journal.usage(session.user_id)
        except StreamLogsRefusedError as exc:
            return ErrorResult(message=str(exc), error_kind=exc.kind)
        except JournalError as exc:
            return ErrorResult(
                message=str(exc), error_kind=StreamLogsErrorKind.NO_JOURNAL
            )

        return TextResult(text=UsageReport(usage, session.thread_id).render())

    @classmethod
    def cleanup(cls, thread_id: str) -> ToolResult:
        try:
            freed = cls._purge(thread_id)
        except StreamLogsRefusedError as exc:
            return ErrorResult(message=str(exc), error_kind=exc.kind)
        except JournalError as exc:
            return ErrorResult(
                message=str(exc), error_kind=StreamLogsErrorKind.PURGE_FAILED
            )

        if freed == 0:
            return ErrorResult(
                message=f"no journals found for thread {thread_id}",
                error_kind=StreamLogsErrorKind.NOT_FOUND,
            )

        return TextResult(
            text=f"journals of thread {thread_id} deleted, freed {freed} bytes"
        )

    @classmethod
    def session(cls) -> JournalSession:
        """Журнал и сессия вызова; вне сессии чата операция невозможна."""
        journal = StreamJournalHub.get()

        user_id = current_user_id()
        if user_id is None:
            raise StreamLogsRefusedError(
                StreamLogsErrorKind.NO_SESSION, "no user session"
            )

        thread_id = current_thread_id()
        if thread_id is None:
            raise StreamLogsRefusedError(
                StreamLogsErrorKind.NO_SESSION, "no thread session"
            )

        return JournalSession(
            journal=journal, user_id=str(user_id), thread_id=thread_id
        )

    @classmethod
    def _purge(cls, thread_id: str) -> int:
        session = cls.session()

        if thread_id == session.thread_id:
            raise StreamLogsRefusedError(
                StreamLogsErrorKind.CURRENT_THREAD,
                "the current thread cannot be purged",
            )

        if thread_id in session.journal.registry.live_threads():
            raise StreamLogsRefusedError(
                StreamLogsErrorKind.LIVE_THREAD,
                f"thread {thread_id} has running tools, try later",
            )

        return session.journal.purge_thread(session.user_id, thread_id)


def build_stream_logs_tools(cfg: None) -> list[BaseTool]:
    @tool(response_format="content_and_artifact")
    def stream_logs_usage() -> tuple[str, ToolResult]:
        """Показать занятость тома журналов вывода инструментов."""
        return pack_result(StreamLogsOps.usage())

    @tool(response_format="content_and_artifact")
    def stream_logs_cleanup(
        thread_id: Annotated[
            str,
            Field(min_length=1, description=StreamLogsPrompt.THREAD_ID),
        ],
    ) -> tuple[str, ToolResult]:
        """Удалить журналы вывода инструментов одного треда."""
        return pack_result(StreamLogsOps.cleanup(thread_id))

    stream_logs_usage.description = str(StreamLogsPrompt.USAGE)
    stream_logs_cleanup.description = str(StreamLogsPrompt.CLEANUP)

    return [stream_logs_usage, stream_logs_cleanup]
