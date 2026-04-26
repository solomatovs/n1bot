"""CLI-arguments ConfigSource-реализация.

Симметричен EnvSource /
TomlSource, но получает значения из
Namespace (или любого Mapping[ConfigKey, object]).

Алгоритм мапинга — операторский: каждый CLI сам решает, какой short-flag
смотрит на какой ConfigKey. Никакого автоматического
ConfigKey.parts → --kebab-case-flag — для коротких операторских флагов
такая авто-схема непрактична. Биндинги декларируются явно
(--persist-path → ConfigKey("ext","chromadb","persist_path")).

Обычно CliArgsSource идёт первым в
ChainedConfigResolver (highest priority),
чтобы CLI-флаг переопределял env/TOML.

None-значения (флаг не передан) отбрасываются конструктором —
следующие источники в цепочке получают шанс ответить.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from boba.domain.core.config import ConfigKey, ConfigSource

__all__ = [
    "CliArgsSource",
    "CliFlagBinding",
    "from_namespace",
]


class CliArgsSource(ConfigSource):
    """ConfigSource поверх Mapping[ConfigKey, object].

    Принимает уже распакованный mapping; парсинг argparse → dict живёт
    в каждом CLI отдельно — пакет не лезет в ваш argparse-парсер.
    Хелпер from_namespace — опционально, если хочется готовый
    bridge на argparse.Namespace.
    """

    def __init__(self, args: Mapping[ConfigKey, object | None]) -> None:
        # None отфильтровываем здесь, чтобы потребителю не нужно было
        # дописывать condition вокруг каждого add_argument.
        self._args = {k: v for k, v in args.items() if v is not None}

    def resolve(self, key: ConfigKey) -> object | None:
        return self._args.get(key)


@dataclass(frozen=True)
class CliFlagBinding:
    """Связь CLI-флага и ConfigKey для from_namespace.

    dest — имя атрибута в Namespace (то же, что
    argparse.add_argument(..., dest=...) или auto-derived из имени
    флага: --persist-path → persist_path).
    """

    key: ConfigKey
    dest: str


def from_namespace(
    ns: argparse.Namespace,
    bindings: Iterable[CliFlagBinding],
) -> CliArgsSource:
    """Сборка CliArgsSource из Namespace.

    Удобно когда у CLI заметное число флагов с маппингом на
    ConfigKey — alternative inline CliArgsSource({key: ns.x, ...})
    тоже работает и для пары флагов чище.
    """
    args: dict[ConfigKey, object | None] = {}
    for b in bindings:
        args[b.key] = getattr(ns, b.dest, None)
    return CliArgsSource(args)
