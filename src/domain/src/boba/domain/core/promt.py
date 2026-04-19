from abc import abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Generic, Self, TypeVar

from boba.domain.core.patterns import (
    FoldFactory,
    Id,
    PrioritySource,
)

TCtx = TypeVar("TCtx")


@dataclass(frozen=True)
class PromptBlock:
    """Один собранный блок промпта."""

    name: str
    content: str


class PromtState(Generic[TCtx]):
    def __init__(self, ctx: TCtx) -> None:
        self.ctx = ctx
        self.blocks = []


TPromtState = TypeVar("TPromtState", bound=PromtState)


class PromptId(Id[str]):
    """Идентификатор провайдера."""

    def to_wire(self) -> str:
        return self._name

    @classmethod
    def from_wire(cls, value: str) -> Self:
        return cls(value)


class PromptResult(PromptBlock):
    """Результат сборки промпта из одного или нескольких провайдеров."""

    def __init__(self, blocks: Iterable[PromptBlock]) -> None:
        self._blocks = list(blocks)

    def to_string(self) -> str:
        """Конкатенация всех непустых блоков."""
        return "\n\n".join(b.content for b in self._blocks if b.content)

    def __iter__(self) -> Iterator[PromptBlock]:
        return iter(self._blocks)


class PromptProvider(PrioritySource[PromptId, PromtState]):
    """
    Провайдер промпта без внешнего контекста.
    Поставляет ноль или более блоков, которые будут конкатенированы
    с другими в итоговый промпт.
    """

    @abstractmethod
    def blocks(self, state: PromtState) -> Iterable[PromptBlock]: ...

    def apply(self, state: PromtState) -> PromtState:
        state.blocks.extend(self.blocks(state))
        return state


class PromptFactory(FoldFactory[PromptId, PromtState[TCtx], PromptResult]):
    """Сервис для сборки user prompt. Тип контекста определяется при использовании."""

    def __init__(self, ctx: TCtx, providers: Iterable[PromptProvider]):
        self._ctx = ctx

        for p in providers:
            self.register(p)

    def initial(self) -> PromtState:
        return PromtState(self._ctx)

    def finalize(self, state: PromtState) -> PromptResult:
        return PromptResult(state.blocks)
