from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import Iterator, Iterable, Generic, TypeVar

from boba.domain.core.patterns import Id

TPromptContext = TypeVar("TPromptContext")


@dataclass(frozen=True)
class PromptBlock:
    """Один собранный блок промпта."""

    name: str
    content: str


class PromptId(Id[str]):
    """Идентификатор провайдера."""


class PromptProvider(ABC, Generic[TPromptContext]):
    """
    Провайдер промпта.
    Поставляет блок, который будет конкатенирован с другими в итоговый промпт.
    """

    @abstractmethod
    def id(self) -> PromptId: ...

    @abstractmethod
    def priority(self) -> int: ...

    @abstractmethod
    def build(self, ctx: TPromptContext) -> PromptBlock: ...


class PromptResult:
    """Результат сборки промпта из одного или нескольких провайдеров."""

    def __init__(self, blocks: Iterable[PromptBlock]) -> None:
        self._blocks = blocks

    def to_string(self) -> str:
        """Конкатенация всех непустых блоков."""
        return "\n\n".join(b.content for b in self._blocks if b.content)

    def __iter__(self) -> Iterator[PromptBlock]:
        return iter(self._blocks)


class PromptService(Generic[TPromptContext]):
    """Сервис для управления промптами от разных провайдеров."""

    def __init__(self) -> None:
        self._providers: dict[PromptId, PromptProvider[TPromptContext]] = {}

    def register(self, provider: PromptProvider[TPromptContext]) -> None:
        """Зарегистрировать провайдер."""
        self._providers[provider.id()] = provider

    def unregister(self, id: PromptId) -> None:
        """Убрать провайдер."""
        self._providers.pop(id, None)

    def providers(self) -> Iterator[PromptProvider[TPromptContext]]:
        return iter(self._providers.values())

    def build(self, ctx: TPromptContext) -> PromptResult:
        """Собрать промпт из всех провайдеров (по priority)."""
        sorted_providers = sorted(self._providers.values(), key=lambda p: p.priority())
        return PromptResult(p.build(ctx) for p in sorted_providers)


class SystemPromptService(PromptService[None]):
    """Сервис для сборки system prompt. Провайдеры не требуют контекста."""


class UserPromptService(PromptService[TPromptContext]):
    """Сервис для сборки user prompt. Тип контекста определяется при использовании."""
