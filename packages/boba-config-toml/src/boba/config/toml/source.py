"""TOML-источник конфига. Mapping ConfigKey → ``[a.b] leaf``."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import tomli

from boba.domain.core.config import ConfigKey, ConfigSource
from boba.domain.core.declaration import FieldSpec

__all__ = [
    "CONFIG_PATH_ENV",
    "TOML_FILE_SUFFIX",
    "TomlFileSource",
    "TomlSource",
    "load_toml",
    "toml_path",
]


logger = logging.getLogger(__name__)


CONFIG_PATH_ENV: Final[str] = "BOBA_CONFIG_PATH"
TOML_FILE_SUFFIX: Final[str] = "_file"


def toml_path(key: ConfigKey) -> tuple[tuple[str, ...], str]:
    """ConfigKey → ``(section_path, leaf)``."""
    return key.parts[:-1], key.parts[-1]


@dataclass(frozen=True)
class _TomlLeaf:
    """Адрес одного листа в TOML-дереве: section_path + leaf."""

    section_path: tuple[str, ...]
    leaf: str

    def dotted(self) -> str:
        """Dotted-форма: ``a.b.leaf``."""
        return ".".join((*self.section_path, self.leaf))

    def with_suffix(self, suffix: str) -> _TomlLeaf:
        """Тот же section_path, leaf с приклеенным suffix."""
        return _TomlLeaf(self.section_path, self.leaf + suffix)


class _TomlSourceBase(ConfigSource):
    """Общий каркас TOML-источников; загружает файл по path или env CONFIG_PATH_ENV."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        strict: bool = False,
        extra_known: Iterable[str] = (),
    ) -> None:
        self._data = load_toml(self._resolve_path(path))
        self._strict = strict
        self._extra_known = frozenset(extra_known)

    @staticmethod
    def _resolve_path(
        path: str | os.PathLike[str] | None,
    ) -> str | os.PathLike[str] | None:
        """path или env CONFIG_PATH_ENV; None если оба пусты."""
        return path if path is not None else os.environ.get(CONFIG_PATH_ENV)

    @staticmethod
    def _toml_lookup(
        data: Mapping[str, Any],
        section_path: Sequence[str],
    ) -> Mapping[str, Any] | None:
        """Спуск по section_path до Mapping; None при промахе на любом уровне."""
        node: Any = data
        for part in section_path:
            if not isinstance(node, Mapping):
                return None
            node = node.get(part)
            if node is None:
                return None
        if not isinstance(node, Mapping):
            return None
        return node

    @classmethod
    def _walk_leaves(
        cls,
        data: Mapping[str, Any],
        path: tuple[str, ...] = (),
    ) -> Iterator[_TomlLeaf]:
        """Рекурсивный обход: yield _TomlLeaf для каждого не-Mapping значения."""
        for key, value in data.items():
            if isinstance(value, Mapping):
                yield from cls._walk_leaves(value, (*path, key))
            else:
                yield _TomlLeaf(path, key)

    @staticmethod
    def _key_to_leaf(key: ConfigKey) -> _TomlLeaf:
        """ConfigKey → _TomlLeaf для сравнения с реальными leaf'ами TOML."""
        section_path, leaf = toml_path(key)
        return _TomlLeaf(section_path, leaf)

    def _report_unknown(self, unknown: list[str], label: str) -> None:
        """warning/ValueError (по strict) на неизвестные leaf'ы; label — категория."""
        if not unknown:
            return
        msg = f"unknown TOML {label}: {sorted(unknown)}"
        if self._strict:
            raise ValueError(msg)
        logger.warning(msg)


class TomlSource(_TomlSourceBase):
    """Значение из TOML-файла; путь — параметр или env CONFIG_PATH_ENV."""

    def bind_schema(
        self,
        items: Iterable[tuple[ConfigKey, FieldSpec[Any]]],
    ) -> None:
        """Сравнить обычные leaf'ы из data со схемой; ругнуться на лишние."""
        known_leaves = {self._key_to_leaf(key) for key, _field in items}
        unknown: list[str] = []
        for actual in self._walk_leaves(self._data):
            if actual.leaf.endswith(TOML_FILE_SUFFIX):
                continue
            if actual in known_leaves:
                continue
            if actual.dotted() in self._extra_known:
                continue
            unknown.append(actual.dotted())
        self._report_unknown(unknown, "keys")

    def resolve(self, key: ConfigKey) -> object | None:
        """Значение под ``[section_path] leaf`` или None."""
        section_path, leaf = toml_path(key)
        section = self._toml_lookup(self._data, section_path)
        if section is None:
            return None
        return section.get(leaf)

    def describe(self, key: ConfigKey) -> str:
        """Operator-readable hint: ``TOML [a.b] leaf (file: $BOBA_CONFIG_PATH)``."""
        section_path, leaf = toml_path(key)
        section_repr = ".".join(section_path) if section_path else "<root>"
        return f"TOML [{section_repr}] {leaf} (file: ${CONFIG_PATH_ENV})"


class TomlFileSource(_TomlSourceBase):
    """Значение из файла под [section] {leaf}_file (Docker-style)."""

    def bind_schema(
        self,
        items: Iterable[tuple[ConfigKey, FieldSpec[Any]]],
    ) -> None:
        """Сравнить ``*_file``-leaf'ы из data со схемой; ругнуться на лишние."""
        known_file_leaves = {
            self._key_to_leaf(key).with_suffix(TOML_FILE_SUFFIX)
            for key, _field in items
        }
        unknown: list[str] = []
        for actual in self._walk_leaves(self._data):
            if not actual.leaf.endswith(TOML_FILE_SUFFIX):
                continue
            if actual in known_file_leaves:
                continue
            if actual.dotted() in self._extra_known:
                continue
            unknown.append(actual.dotted())
        self._report_unknown(unknown, "*_file keys")

    def resolve(self, key: ConfigKey) -> object | None:
        """Прочитать файл, путь к нему — в ``[section_path] leaf_file``."""
        section_path, leaf = toml_path(key)
        section = self._toml_lookup(self._data, section_path)
        if section is None:
            return None
        file_path = section.get(leaf + TOML_FILE_SUFFIX)
        if not isinstance(file_path, str):
            return None
        secret_file = Path(file_path)
        if not secret_file.is_file():
            return None
        return secret_file.read_text(encoding="utf-8").strip()

    def describe(self, key: ConfigKey) -> str:
        """Operator-readable hint: ``TOML [a.b] leaf_file=<path>``."""
        section_path, leaf = toml_path(key)
        section_repr = ".".join(section_path) if section_path else "<root>"
        return (
            f"TOML [{section_repr}] {leaf}{TOML_FILE_SUFFIX}"
            f"=<path-to-secret-file> (file: ${CONFIG_PATH_ENV})"
        )


def load_toml(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Парсит TOML; пустой/несуществующий путь → ``{}``. Битый TOML — наружу."""
    if not path:
        return {}

    p = Path(path)
    if not p.is_file():
        return {}

    with p.open("rb") as f:
        return tomli.load(f)
