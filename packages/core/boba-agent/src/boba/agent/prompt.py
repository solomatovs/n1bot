"""Сборка system-prompt из провайдеров (fold-factory)."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Self

from boba.agent.errors import TerminalError
from boba.agent.events import AgentEvent, PromptFailed
from boba.llm.models import RequestId
from boba.patterns import FoldFactory, Id, PrioritySource


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


class PromptState:
    """Накапливаемое состояние сборки: blocks + контекст."""

    def __init__(self) -> None:
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


class StaticPromptProvider(PromptProvider):
    """Фиксированный текст, зашитый в конфигурацию DI."""

    def __init__(
        self,
        prompt_id: PromptId,
        priority: int,
        content: str,
    ) -> None:
        self._id = prompt_id
        self._priority = priority
        self._content = content

    def id(self) -> PromptId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def blocks(self, state: PromptState) -> Iterable[PromptBlock]:
        yield PromptBlock(name=self._id.name, content=self._content)


class PromptFactory(FoldFactory[PromptId, PromptState, PromptResult]):
    """Собирает PromptResult из зарегистрированных провайдеров."""

    def __init__(self, providers: Iterable[PromptProvider]) -> None:
        super().__init__()
        for p in providers:
            self.register(p)

    def initial(self) -> PromptState:
        return PromptState()

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
