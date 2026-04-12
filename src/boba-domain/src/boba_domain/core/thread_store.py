"""Protocol для хранения thread'ов чата.

ChatThreadStore — CRUD для ChatThread.
ThreadPage — результат постраничного запроса.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from boba_domain.chat.thread import ChatStep, ChatThread, StepFeedback


@dataclass(frozen=True)
class ThreadPage:
    """Страница результатов list_threads."""

    threads: list[ChatThread]
    has_next: bool


@runtime_checkable
class ChatThreadStore(Protocol):
    """CRUD-хранилище для thread'ов чата."""

    def get_thread(self, thread_id: str) -> ChatThread | None: ...

    def list_threads(
        self,
        limit: int,
        offset: int = 0,
        search: str | None = None,
    ) -> ThreadPage: ...

    def save_thread(self, thread: ChatThread) -> None: ...

    def delete_thread(self, thread_id: str) -> None: ...

    def add_step(self, thread_id: str, step: ChatStep) -> None: ...

    def update_step(self, thread_id: str, step: ChatStep) -> None: ...

    def delete_step(self, thread_id: str, step_id: str) -> None: ...

    def set_feedback(
        self, thread_id: str, step_id: str, feedback: StepFeedback | None
    ) -> None: ...

    def get_favorite_steps(self, user_id: str) -> list[ChatStep]: ...
