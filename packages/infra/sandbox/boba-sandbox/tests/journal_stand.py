"""Журнал и контекст вызова для тестов песочницы: том во временном каталоге.

Каждый запуск песочницы адресуется журналом так же, как в приложении: том
даёт автофикстура сессии, контекст вызова ставится на каждый тест.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from boba.sandbox.journal import DirVault, StreamJournal
from boba.sandbox.runner import ToolCallContext
from boba.toolkit.channels import Channel
from boba.toolkit.workflow import WorkflowOutcome


class JournalStandError(RuntimeError):
    """Стенд журнала не настроен: автофикстура не отработала."""


class JournalStand:
    """Том журнала теста и адрес вызова под него."""

    USER: ClassVar[str] = "tester"
    THREAD: ClassVar[str] = "t1"
    TOOL: ClassVar[str] = "probe"

    READ_BYTES: ClassVar[int] = 4 * 1024 * 1024

    _ROOT: ClassVar[Path | None] = None
    _CALLS: ClassVar[int] = 0

    @classmethod
    def use(cls, root: Path) -> None:
        cls._ROOT = root

    @classmethod
    def root(cls) -> Path:
        root = cls._ROOT
        if root is None:
            raise JournalStandError("journal stand root is not configured")

        return root

    @classmethod
    def journal(cls) -> StreamJournal:
        """Журнал без резерва: вытеснение проверяется отдельными тестами."""
        return StreamJournal(DirVault(str(cls.root())), 0)

    @classmethod
    def text_of(
        cls, outcome: WorkflowOutcome, stage: str, channel: Channel
    ) -> str:
        """Записанный журнал канала стадии: проверка того, что дошло до файла."""
        journal = cls.journal()

        for key in outcome.journals:
            if key.stage != stage:
                continue

            if key.channel is not channel:
                continue

            window = journal.head(key, cls.READ_BYTES)
            if window is None:
                raise JournalStandError(f"journal file is missing: {key.rel_log()}")

            return window.text

        raise JournalStandError(f"no journal for {stage}/{channel.value}")

    @classmethod
    def context(cls) -> ToolCallContext:
        """Новый адрес вызова: call_id уникален в пределах процесса."""
        cls._CALLS += 1

        return ToolCallContext(
            user_id=cls.USER,
            thread_id=cls.THREAD,
            call_id=f"call{cls._CALLS}",
            tool=cls.TOOL,
        )
