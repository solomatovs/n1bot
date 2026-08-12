"""Журнал и адрес вызова для тестов вне сессии чата.

Запуск песочницы адресуется журналом так же, как в приложении: том живёт во
временном каталоге процесса и уходит вместе с ним, контекст вызова ставится
на время теста.

Ошибки: JournalError — том недоступен (см. boba.sandbox.journal).
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar

from boba.sandbox.journal import DirVault, StreamJournal
from boba.sandbox.runner import ToolCallContext


class CallStand:
    """Том журнала процесса и адрес вызова под него."""

    USER: ClassVar[str] = "stand"
    THREAD: ClassVar[str] = "tools"
    TOOL: ClassVar[str] = "stand"
    PREFIX: ClassVar[str] = "boba-journal-"

    _ROOT: ClassVar[str] = ""
    _CALLS: ClassVar[int] = 0

    @classmethod
    def journal(cls) -> StreamJournal:
        """Журнал без резерва: вытеснение тестам инструментов не нужно."""
        return StreamJournal(DirVault(cls.root()), 0)

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

    @classmethod
    @contextmanager
    def bound(cls) -> Iterator[ToolCallContext]:
        """Контекст вызова на время теста: без него песочница не стартует."""
        context = cls.context()
        token = ToolCallContext.set(context)
        try:
            yield context
        finally:
            ToolCallContext.reset(token)

    @classmethod
    def root(cls) -> str:
        if cls._ROOT:
            return cls._ROOT

        root = tempfile.mkdtemp(prefix=cls.PREFIX)
        atexit.register(shutil.rmtree, root, True)
        cls._ROOT = root

        return root
