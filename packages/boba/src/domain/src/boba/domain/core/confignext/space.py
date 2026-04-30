"""
ConfigSpace: контракт чтения плоского конфиг-пространства.

Поля ObjectSchema (FieldSpec/MappingField/ListField) знают только этот
интерфейс — не саму FlatConfig
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from boba.domain.core.confignext.lookup import ConfigLookup
from boba.domain.core.confignext.path import ConfigPath, Segment
from boba.domain.core.confignext.value import ConfigValue

__all__ = ["ConfigSpace"]


class ConfigSpace(Protocol):
    """Минимальный контракт, нужный полям ObjectSchema для чтения значений."""

    def lookup(self, path: ConfigPath) -> ConfigLookup[ConfigValue]:
        """Найти значение в указанном пути."""
        ...

    def child_segments(self, prefix: ConfigPath) -> Sequence[Segment]:
        """Уникальные первые сегменты непосредственно под prefix."""
        ...
