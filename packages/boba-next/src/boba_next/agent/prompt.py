"""Сборка system-prompt из провайдеров (fold-factory)."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Generic, Self, TypeVar

from boba.patterns import FoldFactory, Id, PrioritySource
from boba_next.agent.events import AgentEvent, PromptFailed
from boba_next.errors import TerminalError
from boba_next.llm.models import RequestId

TCtx = TypeVar("TCtx")


class PromptError(TerminalError[RequestId, AgentEvent]):
    """Базовая ошибка сборки промпта."""

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider

    def to_user_feedback(self, request_id: RequestId) -> PromptFailed:
        return PromptFailed(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=str(self),
            provider=self.provider,
        )


class PermanentPromptError(PromptError):
    """Провайдер не может вернуть блок: нет файла/прав, битая конфигурация."""


class PromptProviderError(PermanentPromptError):
    """Провайдер упал на чтении источника (OSError → этот тип)."""


@dataclass(frozen=True)
class PromptBlock:
    """Один собранный блок промпта."""

    name: str
    content: str


class PromptId(Id[str]):
    """Идентификатор провайдера."""

    def to_wire(self) -> str:
        return self._name

    @classmethod
    def from_wire(cls, value: str) -> Self:
        return cls(value)


class PromptState(Generic[TCtx]):
    """Накапливаемое состояние сборки: blocks + контекст."""

    def __init__(self, ctx: TCtx) -> None:
        self.ctx = ctx
        self.blocks: list[PromptBlock] = []

    def add(self, block: PromptBlock) -> None:
        self.blocks.append(block)


class PromptResult:
    """Финальная раскладка собранных блоков."""

    def __init__(self, blocks: Iterable[PromptBlock]) -> None:
        self._blocks: list[PromptBlock] = list(blocks)

    def blocks(self) -> list[PromptBlock]:
        return list(self._blocks)

    def to_string(self) -> str:
        """Конкатенация непустых блоков через двойной перенос."""
        return "\n\n".join(b.content for b in self._blocks if b.content)


class PromptProvider(PrioritySource[PromptId, PromptState]):
    """Провайдер блоков system-prompt."""

    @abstractmethod
    def blocks(self, state: PromptState) -> Iterable[PromptBlock]: ...

    def apply(self, state: PromptState) -> PromptState:
        for block in self.blocks(state):
            state.add(block)
        return state


class PromptFactory(FoldFactory[PromptId, PromptState[TCtx], PromptResult]):
    """Собирает PromptResult из зарегистрированных провайдеров."""

    def __init__(self, ctx: TCtx, providers: Iterable[PromptProvider]) -> None:
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
                    f"{type(e).__name__}: {e}",
                    provider=p.id().to_wire(),
                ) from e
        return self.finalize(state)
