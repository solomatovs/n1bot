from abc import abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Self, TypeVar

from boba.domain.core.patterns import CompositeBuilder, Id, Provider

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


class PromptProvider(Provider[PromptId, TPromptContext, list[PromptBlock]]):
    """
    Провайдер промпта.
    Поставляет блок, который будет конкатенирован с другими в итоговый промпт.
    """

    @abstractmethod
    def block(self, ctx: TPromptContext) -> PromptBlock: ...

    def apply(self, ctx: TPromptContext, state: list[PromptBlock]) -> list[PromptBlock]:
        state.append(self.block(ctx))
        return state


class PromptResult:
    """Результат сборки промпта из одного или нескольких провайдеров."""

    def __init__(self, blocks: Iterable[PromptBlock]) -> None:
        self._blocks = list(blocks)

    def to_string(self) -> str:
        """Конкатенация всех непустых блоков."""
        return "\n\n".join(b.content for b in self._blocks if b.content)

    def __iter__(self) -> Iterator[PromptBlock]:
        return iter(self._blocks)


class PromptService(
    CompositeBuilder[PromptId, TPromptContext, list[PromptBlock], PromptResult],
):
    """Сервис для управления промптами от разных провайдеров."""

    def initial(self, ctx: TPromptContext) -> list[PromptBlock]:
        return []

    def finalize(self, state: list[PromptBlock]) -> PromptResult:
        return PromptResult(state)


class SystemPromptService(PromptService[None]):
    """Сервис для сборки system prompt. Провайдеры не требуют контекста."""


class UserPromptService(PromptService[TPromptContext]):
    """Сервис для сборки user prompt. Тип контекста определяется при использовании."""
