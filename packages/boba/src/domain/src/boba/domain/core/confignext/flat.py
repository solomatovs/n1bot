"""FlatConfig: единое плоское пространство, собранное мержем источников.

Сборка реализована как FoldFactory из patterns.py: каждый source адаптируется
в `_SourceReducer(PrioritySource)`, builder сортирует их по priority и
последовательно складывает snapshot'ы в `_MergeState`. Финализация —
заворачивание накопленного state в frozen FlatConfig.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from boba.domain.core.confignext.lookup import (
    ConfigLookup,
    Found,
    NotFound,
)
from boba.domain.core.confignext.path import ConfigPath, Segment
from boba.domain.core.confignext.source import ConfigSource
from boba.domain.core.confignext.value import ConfigValue
from boba.domain.core.patterns import FoldFactory, PrioritySource, StrId

__all__ = ["FlatConfig", "FlatConfigBuilder"]


@dataclass(frozen=True)
class FlatConfig:
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
        Потребители работают через `mapping_key`, `list_index`
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


@dataclass
class _MergeState:
    """Промежуточное состояние сборки FlatConfig"""

    values: dict[ConfigPath, ConfigValue] = field(default_factory=dict)
    origins: dict[ConfigPath, str] = field(default_factory=dict)


class _SourceReducer(PrioritySource[StrId, _MergeState]):
    """Адаптер: один ConfigSource как стадия FoldFactory."""

    def __init__(self, source: ConfigSource) -> None:
        self._src = source
        self._id = StrId(source.name())

    def id(self) -> StrId:
        return self._id

    def priority(self) -> int:
        return self._src.priority()

    def apply(self, state: _MergeState) -> _MergeState:
        name = self._src.name()
        for path, value in self._src.load().items():
            state.values[path] = value
            state.origins[path] = name
        return state


class FlatConfigBuilder(FoldFactory[StrId, _MergeState, FlatConfig]):
    """FoldFactory-сборщик FlatConfig из набора ConfigSource.

    Каждый source оборачивается в `_SourceReducer` и регистрируется как
    стадия. `build()` сортирует стадии по priority, накапливает state,
    финализирует в FlatConfig.
    """

    def initial(self) -> _MergeState:
        return _MergeState()

    def finalize(self, state: _MergeState) -> FlatConfig:
        return FlatConfig(values=state.values, origins=state.origins)

    @classmethod
    def from_sources(cls, sources: Iterable[ConfigSource]) -> FlatConfig:
        """Удобная фабрика: завернуть source'ы в reducer'ы и собрать FlatConfig."""
        builder = cls()
        for src in sources:
            builder.register(_SourceReducer(src))
        return builder.build()
