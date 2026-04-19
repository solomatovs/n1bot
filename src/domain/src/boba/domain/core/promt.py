from abc import abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Generic, Self, TypeVar

from boba.domain.core.patterns import (
    FoldFactory,
    Id,
    PrioritySource,
)

TCtx = TypeVar("TCtx")


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


class PromtState(Generic[TCtx]):
    def __init__(self, ctx: TCtx) -> None:
        self.ctx = ctx
        self.blocks: dict[PromptKind, list[PromptBlock]] = {}

    def add(self, kind: PromptKind, block: PromptBlock) -> None:
        self.blocks.setdefault(kind, []).append(block)


TPromtState = TypeVar("TPromtState", bound=PromtState)


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


class PromptProvider(PrioritySource[PromptId, PromtState]):
    """
    Провайдер промпта без внешнего контекста.
    Поставляет ноль или более блоков одного типа (:meth:`kind`), которые
    будут собраны в соответствующий раздел итогового :class:`PromptResult`.
    """

    @abstractmethod
    def kind(self) -> PromptKind: ...

    @abstractmethod
    def blocks(self, state: PromtState) -> Iterable[PromptBlock]: ...

    def apply(self, state: PromtState) -> PromtState:
        kind = self.kind()
        for block in self.blocks(state):
            state.add(kind, block)
        return state


class PromptFactory(FoldFactory[PromptId, PromtState[TCtx], PromptResult]):
    """Сервис для сборки промптов. Тип контекста определяется при использовании."""

    def __init__(self, ctx: TCtx, providers: Iterable[PromptProvider]):
        super().__init__()
        self._ctx = ctx

        for p in providers:
            self.register(p)

    def initial(self) -> PromtState:
        return PromtState(self._ctx)

    def finalize(self, state: PromtState) -> PromptResult:
        return PromptResult(state.blocks)
