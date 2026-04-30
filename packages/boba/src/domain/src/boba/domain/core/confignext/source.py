"""ConfigSource: контракт источника плоского снимка конфигурации."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping

from boba.domain.core.confignext.path import ConfigPath
from boba.domain.core.confignext.value import ConfigValue

__all__ = ["ConfigSource"]


class ConfigSource(ABC):
    """Источник конфига: имя, приоритет, плоский снимок."""

    @abstractmethod
    def name(self) -> str:
        """Уникальное имя источника — для origin-трассировки."""
        ...

    @abstractmethod
    def priority(self) -> int:
        """Приоритет при мерже; больше = важнее (last-wins)."""
        ...

    @abstractmethod
    def load(self) -> Mapping[ConfigPath, ConfigValue]:
        """Eager-snapshot всех известных источнику значений."""
        ...

    def describe(self, path: ConfigPath) -> str:
        """Operator-readable hint, как задать значение через этот источник."""
        del path
        return ""
