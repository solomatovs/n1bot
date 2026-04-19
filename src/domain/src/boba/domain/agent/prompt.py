"""Сборка LLM-промптов из провайдеров (fold-factory по :class:`PromptKind`).

Provider'ы (``core-errors``-based) поставляют блоки определённого типа
(``SYSTEM`` / ``USER`` / ``TOOLS_DEFINITION``); :class:`PromptFactory`
агрегирует их по приоритету в :class:`PromptResult`. Middleware
``SystemPromptMiddleware`` / ``UserPromptMiddleware`` в ``meat.py``
используют результат для сборки :class:`LLMRequest`.

Параметр ``TCtx`` — тип контекста, прокидываемого провайдерам. В проекте
это :class:`~boba.domain.agent.models.AgentContext`, но сам модуль
работает с любым типом через generics.
"""

from abc import abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Generic, Self, TypeVar

from boba.domain.core.errors import Retryable, TerminalError
from boba.domain.core.patterns import (
    FoldFactory,
    Id,
    PrioritySource,
)

TCtx = TypeVar("TCtx")


class PromptError(TerminalError):
    """Базовая ошибка сборки промпта.

    Адаптеры-провайдеры промптов (чтение файлов, workspace, git) оборачивают
    свои I/O-исключения в потомков этого класса. Ошибки логики/валидации
    (неверный тип, битая регистрация провайдера) — это баги и должны падать
    напрямую, без конвертации в :class:`PromptError`.
    """

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


class RetryablePromptError(PromptError, Retryable):
    """Временная проблема провайдера (транзиентная I/O-ошибка, локфайл)."""


class PermanentPromptError(PromptError):
    """Провайдер не может вернуть блок: нет файла/прав, сломанная конфигурация."""


class PromptProviderError(PermanentPromptError):
    """Провайдер промпта упал на чтении своего источника."""


class PromptKind(Enum):
    """Тип собираемого промпта."""

    SYSTEM = "system"
    USER = "user"
    TOOLS_DEFINITION = "tools_definition"


@dataclass(frozen=True)
class PromptBlock:
    """Один собранный блок промпта."""

    name: str
    content: str


class PromptState(Generic[TCtx]):
    def __init__(self, ctx: TCtx) -> None:
        self.ctx = ctx
        self.blocks: dict[PromptKind, list[PromptBlock]] = {}

    def add(self, kind: PromptKind, block: PromptBlock) -> None:
        self.blocks.setdefault(kind, []).append(block)


TPromptState = TypeVar("TPromptState", bound=PromptState)


class PromptId(Id[str]):
    """Идентификатор провайдера."""

    def to_wire(self) -> str:
        return self._name

    @classmethod
    def from_wire(cls, value: str) -> Self:
        return cls(value)


class PromptResult:
    """Результат сборки промптов, сгруппированный по типу (kind)."""

    def __init__(
        self, blocks_by_kind: Mapping[PromptKind, Iterable[PromptBlock]]
    ) -> None:
        self._by_kind: dict[PromptKind, list[PromptBlock]] = {
            k: list(v) for k, v in blocks_by_kind.items()
        }

    def blocks(self, kind: PromptKind) -> list[PromptBlock]:
        return list(self._by_kind.get(kind, []))

    def to_string(self, kind: PromptKind) -> str:
        """Конкатенация всех непустых блоков заданного типа."""
        return "\n\n".join(
            b.content for b in self._by_kind.get(kind, []) if b.content
        )


class PromptProvider(PrioritySource[PromptId, PromptState]):
    """
    Провайдер промпта без внешнего контекста.
    Поставляет ноль или более блоков одного типа (:meth:`kind`), которые
    будут собраны в соответствующий раздел итогового :class:`PromptResult`.
    """

    @abstractmethod
    def kind(self) -> PromptKind: ...

    @abstractmethod
    def blocks(self, state: PromptState) -> Iterable[PromptBlock]: ...

    def apply(self, state: PromptState) -> PromptState:
        kind = self.kind()
        for block in self.blocks(state):
            state.add(kind, block)
        return state


class PromptFactory(FoldFactory[PromptId, PromptState[TCtx], PromptResult]):
    """Сервис для сборки промптов. Тип контекста определяется при использовании."""

    def __init__(self, ctx: TCtx, providers: Iterable[PromptProvider]):
        super().__init__()
        self._ctx = ctx

        for p in providers:
            self.register(p)

    def initial(self) -> PromptState:
        return PromptState(self._ctx)

    def finalize(self, state: PromptState) -> PromptResult:
        return PromptResult(state.blocks)

    def build(self) -> PromptResult:
        state = self.initial()
        for p in sorted(self.providers(), key=lambda p: p.priority()):
            try:
                state = p.apply(state)
            except PromptError:
                raise
            except OSError as e:
                raise PromptProviderError(
                    f"{type(e).__name__}: {e}", provider=p.id().to_wire()
                ) from e
        return self.finalize(state)
