"""ReferenceResolver: разрешение ссылок `@{path}` внутри FlatConfig.

Eager-фаза между fold-merge источников и созданием `FlatConfig`. После
resolve в результирующем плоском пространстве ссылок не остаётся —
materializer и lookup'ы работают с уже разрешёнными значениями.

Синтаксис: только полная замена. Значение должно быть строкой,
**целиком** соответствующей паттерну `@{path}`, где `path` — абсолютный
путь в нотации `a.b.c[0].d` (без префикса `$`). Шаблоны вида
`"prefix-@{X}-suffix"` намеренно не поддерживаются — это сохраняет
типобезопасность (можно ссылаться на int, list, dict) и не превращает
конфиг в template-engine.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from boba.config.path import ConfigPath, ConfigPathParseError
from boba.value import ConfigValue, StringValue

__all__ = [
    "DEFAULT_REF_DEPTH_LIMIT",
    "REF_PATTERN",
    "CircularRefError",
    "OriginChain",
    "OriginStep",
    "RefDepthExceededError",
    "RefError",
    "ReferenceResolver",
    "UnresolvedRefError",
]


REF_PATTERN: re.Pattern[str] = re.compile(r"^@\{(?P<path>[^}]+)\}$")
"""Паттерн полной замены: значение должно целиком матчиться."""


DEFAULT_REF_DEPTH_LIMIT: int = 32
"""Максимальная глубина цепочки ссылок до того как поднимется ошибка."""


@dataclass(frozen=True)
class OriginStep:
    """Один шаг в цепочке разрешения: какой path, из какого источника."""

    path: ConfigPath
    source: str


OriginChain = tuple[OriginStep, ...]
"""Полная цепочка от ref-узла к финалу.

Длина 1 — значение пришло напрямую из источника, ссылок не было. Длина >1 —
последовательность шагов: первый шаг — где написана ссылка, последний —
где взято финальное значение.
"""


class RefError(Exception):
    """Базовая ошибка резолва ссылок."""


@dataclass(frozen=True)
class UnresolvedRefError(RefError):
    """Ссылка указывает на несуществующий путь."""

    ref_at: ConfigPath
    target: ConfigPath

    def __str__(self) -> str:
        return (
            f"unresolved reference at {self.ref_at.render()!r}: "
            f"target {self.target.render()!r} not found"
        )


@dataclass(frozen=True)
class CircularRefError(RefError):
    """Цепочка ссылок образует цикл."""

    cycle: tuple[ConfigPath, ...]

    def __str__(self) -> str:
        chain = " → ".join(p.render() for p in self.cycle)
        return f"circular reference: {chain}"


@dataclass(frozen=True)
class RefDepthExceededError(RefError):
    """Превышена допустимая глубина цепочки ссылок."""

    ref_at: ConfigPath
    limit: int

    def __str__(self) -> str:
        return (
            f"reference depth limit ({self.limit}) exceeded "
            f"starting at {self.ref_at.render()!r}"
        )


class ReferenceResolver:
    """Разрешает ссылки `@{path}` в плоском пространстве values."""

    def __init__(self, depth_limit: int = DEFAULT_REF_DEPTH_LIMIT) -> None:
        self._depth_limit = depth_limit

    def resolve(
        self,
        values: Mapping[ConfigPath, ConfigValue],
        origins: Mapping[ConfigPath, str],
    ) -> tuple[
        Mapping[ConfigPath, ConfigValue],
        Mapping[ConfigPath, OriginChain],
    ]:
        """Вернуть копии values/origins с разрешёнными ссылками."""
        resolved_values: dict[ConfigPath, ConfigValue] = {}
        resolved_origins: dict[ConfigPath, OriginChain] = {}
        for path in values:
            value, chain = self._resolve_path(path, values, origins, ())
            resolved_values[path] = value
            resolved_origins[path] = chain
        return resolved_values, resolved_origins

    def _resolve_path(
        self,
        path: ConfigPath,
        values: Mapping[ConfigPath, ConfigValue],
        origins: Mapping[ConfigPath, str],
        visited: tuple[ConfigPath, ...],
    ) -> tuple[ConfigValue, OriginChain]:
        if path in visited:
            raise CircularRefError(cycle=(*visited, path))
        if len(visited) >= self._depth_limit:
            start = visited[0] if visited else path
            raise RefDepthExceededError(ref_at=start, limit=self._depth_limit)

        value = values[path]
        step = OriginStep(path=path, source=origins.get(path, ""))
        target = self._extract_target(value, path)
        if target is None:
            return value, (step,)

        if target not in values:
            ref_at = visited[0] if visited else path
            raise UnresolvedRefError(ref_at=ref_at, target=target)

        sub_value, sub_chain = self._resolve_path(
            target,
            values,
            origins,
            (*visited, path),
        )
        return sub_value, (step, *sub_chain)

    @staticmethod
    def _extract_target(value: ConfigValue, ref_at: ConfigPath) -> ConfigPath | None:
        """Вернуть target-path, если value — ссылка `@{path}`; иначе None."""
        if not isinstance(value, StringValue):
            return None
        match = REF_PATTERN.match(value.text)
        if match is None:
            return None
        body = match.group("path")
        try:
            return ConfigPath.parse("$" + body)
        except ConfigPathParseError as exc:
            raise UnresolvedRefError(
                ref_at=ref_at,
                target=ConfigPath.root(),
            ) from exc
