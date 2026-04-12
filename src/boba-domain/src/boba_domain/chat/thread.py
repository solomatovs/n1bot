"""Доменные типы для thread'ов чата.

ChatThread — один чат (thread) с историей шагов.
ChatStep — один шаг (сообщение, tool call, и т.д.) внутри thread'а.
StepFeedback — оценка шага пользователем.
ThreadMetadata — типизированные метаданные thread'а.
StepType — тип шага.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class StepType(Enum):
    """Тип шага в thread'е."""

    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL = "tool"
    RUN = "run"


@dataclass(frozen=True)
class StepFeedback:
    """Оценка шага пользователем (thumbs up/down)."""

    id: str
    value: int  # 0 = negative, 1 = positive
    comment: str = ""
    strategy: str = "user"


@dataclass(frozen=True)
class ThreadMetadata:
    """Метаданные thread'а — привязка к папке и модели."""

    folder: str = ""
    model: str = ""


@dataclass(frozen=True)
class ChatStep:
    """Один шаг внутри thread'а."""

    id: str
    thread_id: str
    step_type: StepType
    output: str = ""
    input: str = ""
    created_at: str = ""
    name: str = ""
    parent_id: str | None = None
    feedback: StepFeedback | None = None
    is_favorite: bool = False


@dataclass(frozen=True)
class ChatThread:
    """Один thread чата со всеми шагами."""

    id: str
    created_at: str
    name: str | None = None
    user_id: str = "default"
    user_identifier: str = "default"
    metadata: ThreadMetadata = field(default_factory=ThreadMetadata)
    steps: list[ChatStep] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
