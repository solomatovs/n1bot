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
    """Тип шага в thread'е.

    USER_MESSAGE      — сообщение пользователя.
    ASSISTANT_MESSAGE  — ответ ассистента.
    TOOL              — вызов инструмента (tool call / tool result).
    RUN               — системный шаг (напр. on_chat_start, on_settings_update).
    """

    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL = "tool"
    RUN = "run"


@dataclass(frozen=True)
class StepFeedback:
    """Реакция пользователя на ответ ассистента (кнопки 👍/👎 в UI).

    Пользователь нажимает 👍 → value=1, 👎 → value=0.
    Если пользователь не оценивал шаг, feedback остаётся None в ChatStep.
    """

    id: str  # UUID этой оценки
    value: int  # 1 = 👍 положительная, 0 = 👎 отрицательная
    comment: str = ""  # текстовый комментарий к оценке (если UI запрашивает)
    strategy: str = "user"  # источник оценки: "user" = человек


@dataclass(frozen=True)
class ThreadMetadata:
    """Метаданные thread'а — привязка к workspace и модели.

    folder — UUID-имя папки workspace'а на диске.
    model  — идентификатор LLM, выбранной для этого чата.
    """

    folder: str = ""
    model: str = ""


@dataclass(frozen=True)
class ChatStep:
    """Один шаг внутри thread'а.

    Каждое событие в переписке — это шаг: сообщение пользователя,
    ответ ассистента, вызов инструмента, системный запуск.
    """

    id: str  # UUID шага
    thread_id: str  # UUID чата, к которому привязан шаг
    step_type: StepType  # тип шага
    output: str = ""  # текст ответа / результат tool
    input: str = ""  # текст вопроса / аргументы tool
    created_at: str = ""  # ISO timestamp создания
    name: str = ""  # имя (ассистент, tool name, callback name)
    parent_id: str | None = None  # UUID родителя для вложенных шагов
    feedback: StepFeedback | None = None  # оценка пользователем (если есть)
    is_favorite: bool = False  # помечен ли как избранный


@dataclass(frozen=True)
class ChatThread:
    """Один thread чата со всеми шагами.

    Thread = один чат = один workspace. Связь с workspace
    хранится в metadata.folder. Все шаги чата — в steps.
    """

    id: str  # UUID thread'а (совпадает с Chainlit thread_id)
    created_at: str  # ISO timestamp создания
    name: str  # отображаемое имя в sidebar (обязательное)
    user_id: str = "default"  # внутренний ID пользователя
    user_identifier: str = "default"  # логин / идентификатор пользователя
    metadata: ThreadMetadata = field(default_factory=ThreadMetadata)
    steps: list[ChatStep] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    _AUTO_NAME_PREFIX = "workspace-"

    def has_auto_name(self) -> bool:
        """Имя назначено автоматически (workspace-N) и ещё не переименовано."""
        return self.name.startswith(self._AUTO_NAME_PREFIX)

    def matches_search(self, query: str) -> bool:
        """Имя thread'а содержит поисковый запрос (регистронезависимо)."""
        return query.lower() in self.name.lower()
