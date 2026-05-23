"""События tool-слоя: прогресс long-running tools + terminal-результат.

Tool-author может писать tool в двух стилях:

1. **execute-стиль** (по умолчанию) — `@tool def f(...) -> X: return ...`.
   Framework сам обернёт результат в один `ToolStreamCompleted` event,
   так что снаружи interface всегда streaming.

2. **stream-стиль** — `@tool def f(...) -> Generator[ToolEvent, None, X]: ...`,
   tool сам yield-ит `ToolProgressReported` по ходу работы и возвращает
   финальный результат через `return X` (StopIteration.value). Framework
   подхватит `return`-значение и завернёт в `ToolStreamCompleted` за tool'а.

Граница слоёв: ToolEvent **не знает** про agent-слой и `AgentEvent`. Маппинг
ToolEvent → AgentEvent делает converter в agent-middleware (по аналогии с
LLMEvent → AgentEvent через `LLMToAgentConverter`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias

from boba.tools.domain.result import ToolResult

__all__ = [
    "BaseToolEvent",
    "ToolEvent",
    "ToolProgressReported",
    "ToolSeverity",
    "ToolStreamCompleted",
]


class ToolSeverity(StrEnum):
    """Уровень события tool-слоя.

    Маппится converter'ом в `boba.agent.events.Severity` 1-к-1.
    Локальный enum — чтобы tool-domain не зависел от agent-слоя.
    """

    INFO = "info"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True)
class BaseToolEvent:
    """Базовый класс всех событий tool-слоя.

    Sealed-union через `ToolEvent` TypeAlias — для `match`/`assert_never`
    в converter'ах. Сам по себе не ABC: при `match`-разборе нужны только
    конкретные классы, а isinstance-check используется для дискриминации
    «это ToolEvent vs мусор» в `DishkaTool.stream`.
    """


@dataclass(frozen=True)
class ToolProgressReported(BaseToolEvent):
    """Прогресс выполнения long-running tool'а.

    `headline` — короткая строка для UI (например `indexed 12/100 pages`).
    `details` — структурированные данные (k→v строк), которые показываются
    в развёрнутом виде. `severity` — визуальный приоритет.

    Tool-author эмитит это событие просто `yield ToolProgressReported(...)`
    внутри generator-tool'а. Converter в middleware превратит его в
    `ToolProgress` PhaseEvent с привязкой к `tool_call_id`.
    """

    headline: str
    details: Mapping[str, str] = field(default_factory=dict)
    severity: ToolSeverity = ToolSeverity.INFO


@dataclass(frozen=True)
class ToolStreamCompleted(BaseToolEvent):
    """Терминальное событие tool-stream'а — несёт финальный результат.

    Для execute-стиля tool'ов framework сам генерирует одно такое событие
    из возврата `execute(...)`. Для stream-стиля tool'ов framework
    собирает `result` из `return`-значения generator'а (StopIteration.value)
    и эмитит `ToolStreamCompleted` после исчерпания yield-потока.

    Middleware ловит это событие как маркер «конец stream'а» и
    конвертирует в `ToolResultReady`.
    """

    result: ToolResult


ToolEvent: TypeAlias = ToolProgressReported | ToolStreamCompleted
"""Sealed union всех событий tool-слоя.

Используется как тип yield'а в `Tool.stream(...)` / `ToolExecutor.stream(...)`
и в converter'ах. При добавлении нового события сюда — `match`-statement'ы
в converter'ах должны быть расширены (статически проверяется через
`assert_never` в `_`-branch'е).
"""
