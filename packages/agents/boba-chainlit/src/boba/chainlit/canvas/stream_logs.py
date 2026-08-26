"""Tools stream_logs: занятость тома журналов вывода и уборка тредов.

Журналы пишутся автоматически на каждый потоковый вызов инструмента; здесь
LLM смотрит, чем занят том пользователя, и освобождает место осознанно —
вместо слепого LRU-вытеснения.

Ошибки: ErrorResult — нет сессии или журнала, тред занят или не найден;
остальное упаковывает ToolErrorGuard.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from langchain_core.tools import BaseTool, tool
from pydantic import Field

from boba.canvas.journal import (
    StreamJournalError,
    StreamJournalHub,
    StreamStorePort,
    VaultUsage,
)
from boba.identity.context import CallContext
from boba.identity.errors import RefusalError
from boba.toolkit.result import ErrorResult, TextResult, ToolResult, pack_result
from boba.toolrun.streams import ToolStreams

__all__ = [
    "StreamLogsErrorKind",
    "StreamLogsOps",
    "StreamLogsPrompt",
    "StreamLogsRefusedError",
    "UsageReport",
    "build_stream_logs_tools",
]


class StreamLogsErrorKind(StrEnum):
    """Коды отказов stream_logs: уезжают в ErrorResult.error_kind."""

    NO_JOURNAL = "no_journal"
    CURRENT_THREAD = "current_thread"
    LIVE_THREAD = "live_thread"
    NOT_FOUND = "thread_not_found"
    PURGE_FAILED = "purge_failed"


class StreamLogsRefusedError(RefusalError):
    """Операция над журналами отклонена; текст причины готов для LLM."""


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
class StreamLogsOps:
    """Операции над журналами в текущей сессии: отчёт и уборка треда."""

    journal: StreamStorePort
    user_id: str
    thread_id: str

    @classmethod
    def resolve(cls) -> StreamLogsOps:
        """Собрать область вызова; нет журнала или контекста — отказ для LLM."""
        journal = StreamJournalHub.get()
        if journal is None:
            raise StreamLogsRefusedError(
                StreamLogsErrorKind.NO_JOURNAL,
                "stream journal is disabled in the app config",
            )

        context = CallContext.current()
        return cls(
            journal=journal,
            user_id=context.subject.user_key,
            thread_id=context.scope.id,
        )

    def usage_text(self) -> str:
        usage = self.journal.usage(self.user_id)

        return UsageReport(usage, self.thread_id).render()

    def purge(self, thread_id: str) -> str:
        """Снести журналы треда; занятый или пустой тред — отказ для LLM."""
        if thread_id == self.thread_id:
            raise StreamLogsRefusedError(
                StreamLogsErrorKind.CURRENT_THREAD,
                "the current thread cannot be purged",
            )

        if thread_id in ToolStreams.live_scopes():
            raise StreamLogsRefusedError(
                StreamLogsErrorKind.LIVE_THREAD,
                f"thread {thread_id} has running tools, try later",
            )

        freed = self.journal.purge_thread(self.user_id, thread_id)
        if freed == 0:
            raise StreamLogsRefusedError(
                StreamLogsErrorKind.NOT_FOUND,
                f"no journals found for thread {thread_id}",
            )

        return f"journals of thread {thread_id} deleted, freed {freed} bytes"


def build_stream_logs_tools(cfg: None) -> list[BaseTool]:
    @tool(response_format="content_and_artifact")
    def stream_logs_usage() -> tuple[str, ToolResult]:
        """Показать занятость тома журналов вывода инструментов."""
        try:
            text = StreamLogsOps.resolve().usage_text()
        except RefusalError as e:
            return pack_result(ErrorResult(message=str(e), error_kind=e.kind))
        except StreamJournalError as e:
            return pack_result(
                ErrorResult(message=str(e), error_kind=StreamLogsErrorKind.NO_JOURNAL)
            )

        return pack_result(TextResult(text=text))

    @tool(response_format="content_and_artifact")
    def stream_logs_cleanup(
        thread_id: Annotated[
            str,
            Field(min_length=1, description=StreamLogsPrompt.THREAD_ID),
        ],
    ) -> tuple[str, ToolResult]:
        """Удалить журналы вывода инструментов одного треда."""
        try:
            text = StreamLogsOps.resolve().purge(thread_id)
        except RefusalError as e:
            return pack_result(ErrorResult(message=str(e), error_kind=e.kind))
        except StreamJournalError as e:
            return pack_result(
                ErrorResult(
                    message=str(e),
                    error_kind=StreamLogsErrorKind.PURGE_FAILED,
                )
            )

        return pack_result(TextResult(text=text))

    stream_logs_usage.description = str(StreamLogsPrompt.USAGE)
    stream_logs_cleanup.description = str(StreamLogsPrompt.CLEANUP)

    return [stream_logs_usage, stream_logs_cleanup]
