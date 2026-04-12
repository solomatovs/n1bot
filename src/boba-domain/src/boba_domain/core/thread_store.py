"""Protocol для хранения thread'ов чата.

ChatThreadStore — CRUD для ChatThread.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from boba_domain.chat.thread import ChatStep, ChatThread, StepFeedback


@runtime_checkable
class ChatThreadStore(Protocol):
    """CRUD-хранилище для thread'ов чата."""

    def get_thread(self, thread_id: str) -> ChatThread: ...

    def iter_threads(
        self,
        search: str | None = None,
    ) -> Iterator[ChatThread]: ...

    def save_thread(self, thread: ChatThread) -> None: ...

    def delete_thread(self, thread_id: str) -> None: ...

    def add_step(self, step: ChatStep) -> None: ...

    def update_step(self, step: ChatStep) -> None: ...

    def delete_step(self, thread_id: str, step_id: str) -> None: ...

    def set_feedback(
        self, thread_id: str, step_id: str, feedback: StepFeedback | None
    ) -> None: ...

    def get_favorite_steps(self, user_id: str) -> list[ChatStep]: ...
