"""FlatConfig: единое плоское пространство значений конфига.

Сборка из источников живёт в `bundle.py` (ConfigBundleFactory как FoldFactory).
Этот модуль содержит только структуру данных и базовые lookup-операции.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from boba.config.path import (
    ConfigLookup,
    ConfigPath,
    ConfigSpace,
    Found,
    NotFound,
    Segment,
)
from boba.value import ConfigValue

__all__ = ["FlatConfig"]


@dataclass(frozen=True)
class FlatConfig(ConfigSpace):
    """Единый плоский снимок конфига после мержа всех источников."""

    values: Mapping[ConfigPath, ConfigValue]
    origins: Mapping[ConfigPath, str]

    def lookup(self, path: ConfigPath) -> ConfigLookup[ConfigValue]:
        if path in self.values:
            return Found(self.values[path])
        return NotFound()

    def origin_of(self, path: ConfigPath) -> ConfigLookup[str]:
        if path in self.origins:
            return Found(self.origins[path])
        return NotFound()

    def keys_under(self, prefix: ConfigPath) -> Iterable[ConfigPath]:
        """Все известные leaf-пути под prefix (включая глубокие)."""
        for path in self.values:
            if path.startswith(prefix) and path != prefix:
                yield path

    def child_segments(self, prefix: ConfigPath) -> Sequence[Segment]:
        """Уникальные первые сегменты непосредственно под prefix.

        Стабильный порядок: сначала встретившиеся первыми.
        Потребители работают через `mapping_key`, `list_index`.
        """
        seen: list[Segment] = []
        seen_set: set[Segment] = set()
        prefix_len = len(prefix)
        for path in self.values:
            if not path.startswith(prefix):
                continue
            if len(path) <= prefix_len:
                continue
            head = path[prefix_len]
            if head in seen_set:
                continue
            seen.append(head)
            seen_set.add(head)
        return tuple(seen)

    def subtree(self, prefix: ConfigPath) -> Mapping[ConfigPath, ConfigValue]:
        """Плоский срез под prefix (ключи остаются абсолютными)."""
        return {p: v for p, v in self.values.items() if p.startswith(prefix)}
