from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import Iterator, Iterable


@dataclass(frozen=True)
class PromptBlock:
    """Один собранный блок промпта."""

    name: str
    content: str


class PromptId:
    """Идентификатор провайдера."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PromptId) and self._name == other._name

    def __hash__(self) -> int:
        return hash(self._name)

    def __repr__(self) -> str:
        return f"PromptId({self._name!r})"


class PromptProvider(ABC):
    """
    Провайдер промпта.
    Поставляет блок, который будет конкатенирован с другими в итоговый промпт.
    """

    @abstractmethod
    def id(self) -> PromptId: ...

    @abstractmethod
    def priority(self) -> int: ...

    @abstractmethod
    def build(self) -> PromptBlock: ...


class PromptResult:
    """Результат сборки промпта из одного или нескольких провайдеров."""

    def __init__(self, blocks: Iterable[PromptBlock]) -> None:
        self._blocks = blocks

    def build(self) -> str:
        """Конкатенация всех непустых блоков."""
        return "\n\n".join(b.content for b in self._blocks if b.content)

    def __iter__(self) -> Iterator[PromptBlock]:
        return iter(self._blocks)


class PromptService:
    """Сервис для управления промптами от разных провайдеров."""

    def __init__(self) -> None:
        self._providers: dict[PromptId, PromptProvider] = {}

    def register(self, provider: PromptProvider) -> None:
        """Зарегистрировать провайдер."""
        self._providers[provider.id()] = provider

    def unregister(self, id: PromptId) -> None:
        """Убрать провайдер."""
        self._providers.pop(id, None)

    def providers(self) -> Iterator[PromptProvider]:
        return iter(self._providers.values())

    def build(self) -> PromptResult:
        """Собрать промпт из всех провайдеров (по priority)."""
        sorted_providers = sorted(self._providers.values(), key=lambda p: p.priority())
        return PromptResult(p.build() for p in sorted_providers)


class SystemPromptService(PromptService):
    """Сервис для сборки system prompt."""


class UserPromptService(PromptService):
    """Сервис для сборки user prompt (контекст: IDE selection, template и т.д.)."""
