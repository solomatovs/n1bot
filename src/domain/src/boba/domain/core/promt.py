from abc import abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Self, TypeVar

from boba.domain.core.patterns import (
    ContextFoldFactory,
    ContextPrioritySource,
    FoldFactory,
    Id,
    PrioritySource,
)

TPromptContext = TypeVar("TPromptContext")


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


class PromptResult:
    """Результат сборки промпта из одного или нескольких провайдеров."""

    def __init__(self, blocks: Iterable[PromptBlock]) -> None:
        self._blocks = list(blocks)

    def to_string(self) -> str:
        """Конкатенация всех непустых блоков."""
        return "\n\n".join(b.content for b in self._blocks if b.content)

    def __iter__(self) -> Iterator[PromptBlock]:
        return iter(self._blocks)


class PromptProvider(PrioritySource[PromptId, list[PromptBlock]]):
    """
    Провайдер промпта без внешнего контекста.
    Поставляет ноль или более блоков, которые будут конкатенированы
    с другими в итоговый промпт.
    """

    @abstractmethod
    def blocks(self) -> Iterable[PromptBlock]: ...

    def apply(self, state: list[PromptBlock]) -> list[PromptBlock]:
        state.extend(self.blocks())
        return state


class ContextPromptProvider(
    ContextPrioritySource[PromptId, TPromptContext, list[PromptBlock]],
):
    """
    Провайдер промпта, которому нужен контекст запроса
    (например, текст пользователя, выделение в IDE).
    """

    @abstractmethod
    def blocks(self, ctx: TPromptContext) -> Iterable[PromptBlock]: ...

    def ctx(self, ctx: TPromptContext) -> PromptProvider:
        return _BoundPromptProvider(self, ctx)


class _BoundPromptProvider(PromptProvider):
    def __init__(
        self,
        source: "ContextPromptProvider[Any]",
        ctx: Any,
    ) -> None:
        self._source = source
        self._ctx = ctx

    def id(self) -> PromptId:
        return self._source.id()

    def priority(self) -> int:
        return self._source.priority()

    def blocks(self) -> Iterable[PromptBlock]:
        return self._source.blocks(self._ctx)


class SystemPromptService(
    FoldFactory[PromptId, list[PromptBlock], PromptResult],
):
    """Сервис для сборки system prompt. Провайдеры не требуют контекста."""

    def initial(self) -> list[PromptBlock]:
        return []

    def finalize(self, state: list[PromptBlock]) -> PromptResult:
        return PromptResult(state)


class UserPromptService(
    ContextFoldFactory[PromptId, TPromptContext, list[PromptBlock], PromptResult],
):
    """Сервис для сборки user prompt. Тип контекста определяется при использовании."""

    def initial(self, ctx: TPromptContext) -> list[PromptBlock]:
        return []

    def finalize(self, state: list[PromptBlock]) -> PromptResult:
        return PromptResult(state)
