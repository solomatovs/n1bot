from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import Iterator


@dataclass(frozen=True)
class SystemPromptBlock:
    """Один собранный блок системного промпта."""

    name: str
    content: str


class SystemPromptId:
    """Идентификатор провайдера."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SystemPromptId) and self._name == other._name

    def __hash__(self) -> int:
        return hash(self._name)

    def __repr__(self) -> str:
        return f"SystemPromptId({self._name!r})"


class SystemPromptProvider(ABC):
    """
    Провайдер системного промпта.
    Поставляет один или несколько блоков, которые будут конкатенированы в итоговый системный промпт.
    """

    @abstractmethod
    def id(self) -> SystemPromptId: ...

    @abstractmethod
    def priority(self) -> int: ...

    @abstractmethod
    def build(self) -> SystemPromptBlock: ...



class SystemPromptResult:
    """Результат сборки системного промпта из одного или нескольких провайдеров."""

    def __init__(self, blocks: Iterator[SystemPromptBlock]) -> None:
        self._blocks = blocks

    def build(self) -> str:
        """Конкатенация всех непустых блоков."""
        return "\n\n".join(b.content for b in self._blocks if b.content)

    def __iter__(self) -> Iterator[SystemPromptBlock]:
        return iter(self._blocks)


class SystemPromptService:
    """Сервис для управления системными промптами от разных провайдеров."""

    def register(self, provider: SystemPromptProvider) -> None:
        """Зарегистрировать провайдер, вызвать provider.enter()."""
        ...

    def unregister(self, id: SystemPromptId) -> None:
        """Вызвать provider.close() и убрать провайдер."""
        ...

    def providers(self) -> Iterator[SystemPromptProvider]: ...

    def build(self) -> SystemPromptResult:
        """Собрать system prompt из всех провайдеров (по priority)."""
        ...
