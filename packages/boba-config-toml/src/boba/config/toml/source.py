"""TOML ConfigSource-реализации.

Мапинг ConfigKey → TOML-путь: parts[:-1] — секции, parts[-1] — leaf.
ConfigKey("ext","chromadb","persist_path") → [ext.chromadb] persist_path.

TomlFileSource — TOML-вариант Docker-style секрета: значение — путь к
файлу под ключом {leaf}_file, содержимое читается и trailing-whitespace
обрезается.

Источники автономны (симметрично EnvSource): на старте TomlSource() и
TomlFileSource() сами читают путь из env CONFIG_PATH_ENV, парсят TOML и
кешируют данные. Оператор может явно передать path в конструктор —
полезно для тестов или нестандартного расположения. Если путь не задан
или файл не существует — пустые данные, источник тихо отдаёт None и
следующий в цепочке отвечает.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

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
"""Имя env-переменной, указывающей путь к TOML-файлу приложения.

Читается конструкторами TomlSource/TomlFileSource при создании.
"""

TOML_FILE_SUFFIX: Final[str] = "_file"
"""Суффикс leaf-ключа в TOML для секрет-указателя на файл."""


def toml_path(key: ConfigKey) -> tuple[tuple[str, ...], str]:
    """ConfigKey → (section_path, leaf_key) для TOML-навигации.

    Чистая функция, доступна публично — пригодится для генерации
    operator-доки и сообщений об ошибках («задайте в TOML под
    [{'.'.join(section)}] {leaf}»).
    """
    return key.parts[:-1], key.parts[-1]


def _toml_lookup(
    data: Mapping[str, Any], section_path: Sequence[str]
) -> Mapping[str, Any] | None:
    """Спускается по вложенным TOML-таблицам; возвращает None если
    путь не найден или промежуточный узел не Mapping.
    """
    node: Any = data
    for p in section_path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(p)
        if node is None:
            return None
    if not isinstance(node, Mapping):
        return None
    return node


@dataclass(frozen=True)
class _TomlLeaf:
    """Одно листовое значение, найденное в TOML-данных.

    section_path — кортеж имён вложенных таблиц до листа;
    leaf — имя ключа листа в его таблице. Полная адресация:
    ``[a.b.c] leaf`` ↔ ``_TomlLeaf(section_path=('a','b','c'), leaf='leaf')``.
    """

    section_path: tuple[str, ...]
    leaf: str

    def dotted(self) -> str:
        """Operator-readable вид: ``a.b.c.leaf`` или ``leaf`` для root."""
        return ".".join((*self.section_path, self.leaf))

    def with_suffix(self, suffix: str) -> _TomlLeaf:
        """Тот же section_path, но leaf с приклеенным suffix.

        Используется для перевода между ConfigKey-leaf и его *_file
        вариантом (TomlFileSource): значение под ``[ext.chromadb]
        persist_path`` против пути под ``[ext.chromadb] persist_path_file``.
        """
        return _TomlLeaf(self.section_path, self.leaf + suffix)


def _walk_leaves(
    data: Mapping[str, Any],
    path: tuple[str, ...] = (),
) -> Iterator[_TomlLeaf]:
    """Рекурсивно обходит TOML-таблицы; yield-ит _TomlLeaf для каждого
    значения, не являющегося Mapping.

    Списки и list-of-dict-arrays (``[[foo]]``) трактуются как leaf'ы
    (не спускаемся в них) — у схемы конфига нет адресации внутри
    list-элементов.
    """
    for key, value in data.items():
        if isinstance(value, Mapping):
            yield from _walk_leaves(value, path + (key,))
        else:
            yield _TomlLeaf(path, key)


def _key_to_leaf(key: ConfigKey) -> _TomlLeaf:
    """Перевод ConfigKey → _TomlLeaf для сравнения известных ключей
    схемы со значениями, найденными при обходе TOML."""
    section_path, leaf = toml_path(key)
    return _TomlLeaf(section_path, leaf)


def _resolve_path(path: str | os.PathLike[str] | None) -> str | os.PathLike[str] | None:
    """Если path не задан — берём из env CONFIG_PATH_ENV."""
    return path if path is not None else os.environ.get(CONFIG_PATH_ENV)


class TomlSource(ConfigSource):
    """Значение из TOML-файла по пути из ConfigKey.

    На старте читает файл (по умолчанию — из env CONFIG_PATH_ENV) и
    кеширует. Любые типы из TOML проходят как есть (int/str/bool/
    list/...) — конвертер FieldSpec сам их разберёт.

    На bind_schema опционально проверяет TOML-ключи на опечатки —
    leaf'ы, не соответствующие ни одному зарегистрированному
    ConfigKey. Leaf'ы с суффиксом ``_file`` исключаются из проверки —
    это территория TomlFileSource. См. ``strict`` / ``extra_known`` —
    семантика та же, что у :class:`boba.config.env.EnvSource`.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        strict: bool = False,
        extra_known: Iterable[str] = (),
    ) -> None:
        self._data = load_toml(_resolve_path(path))
        self._strict = strict
        self._extra_known = frozenset(extra_known)

    def bind_schema(
        self,
        items: Iterable[tuple[ConfigKey, FieldSpec[Any]]],
    ) -> None:
        known_leaves = {_key_to_leaf(key) for key, _field in items}
        unknown: list[str] = []
        for actual in _walk_leaves(self._data):
            if actual.leaf.endswith(TOML_FILE_SUFFIX):
                continue  # *_file leaf'ы — забота TomlFileSource
            if actual in known_leaves:
                continue
            if actual.dotted() in self._extra_known:
                continue
            unknown.append(actual.dotted())
        if not unknown:
            return
        msg = (
            f"unknown TOML keys (typo? schema not registered?): "
            f"{sorted(unknown)}"
        )
        if self._strict:
            raise ValueError(msg)
        logger.warning(msg)

    def resolve(self, key: ConfigKey) -> object | None:
        section_path, leaf = toml_path(key)
        section = _toml_lookup(self._data, section_path)
        if section is None:
            return None
        return section.get(leaf)

    def describe(self, key: ConfigKey) -> str:
        section_path, leaf = toml_path(key)
        section_repr = ".".join(section_path) if section_path else "<root>"
        return f"TOML [{section_repr}] {leaf} (file: ${CONFIG_PATH_ENV})"


class TomlFileSource(ConfigSource):
    """Значение из файла, путь к которому хранит TOML-ключ {leaf}_file
    в той же секции, что и TomlSource для leaf.

    Если ключ отсутствует или файл не существует — None (последующие
    источники продолжают). Содержимое возвращается с обрезанным
    trailing-whitespace.

    bind_schema симметричен TomlSource'у: проверяет TOML-leaf'ы с
    суффиксом ``_file`` на соответствие схеме (после strip'а суффикса).
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        strict: bool = False,
        extra_known: Iterable[str] = (),
    ) -> None:
        self._data = load_toml(_resolve_path(path))
        self._strict = strict
        self._extra_known = frozenset(extra_known)

    def bind_schema(
        self,
        items: Iterable[tuple[ConfigKey, FieldSpec[Any]]],
    ) -> None:
        # На каждый ConfigKey-leaf TomlFileSource ожидает <leaf>_file
        # в той же секции — по нему он читает путь к секрету.
        known_file_leaves = {
            _key_to_leaf(key).with_suffix(TOML_FILE_SUFFIX)
            for key, _field in items
        }
        unknown: list[str] = []
        for actual in _walk_leaves(self._data):
            if not actual.leaf.endswith(TOML_FILE_SUFFIX):
                continue  # обычные leaf'ы — забота TomlSource
            if actual in known_file_leaves:
                continue
            if actual.dotted() in self._extra_known:
                continue
            unknown.append(actual.dotted())
        if not unknown:
            return
        msg = (
            f"unknown TOML *_file keys (typo? schema not registered?): "
            f"{sorted(unknown)}"
        )
        if self._strict:
            raise ValueError(msg)
        logger.warning(msg)

    def resolve(self, key: ConfigKey) -> object | None:
        section_path, leaf = toml_path(key)
        section = _toml_lookup(self._data, section_path)
        if section is None:
            return None
        file_path = section.get(leaf + TOML_FILE_SUFFIX)
        if not isinstance(file_path, str):
            return None
        p = Path(file_path)
        if not p.is_file():
            return None
        return p.read_text(encoding="utf-8").strip()

    def describe(self, key: ConfigKey) -> str:
        section_path, leaf = toml_path(key)
        section_repr = ".".join(section_path) if section_path else "<root>"
        return (
            f"TOML [{section_repr}] {leaf}{TOML_FILE_SUFFIX}"
            f"=<path-to-secret-file> (file: ${CONFIG_PATH_ENV})"
        )


def load_toml(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Читает TOML-файл по пути; пустая/несуществующая — пустой dict.

    Битый TOML не глотаем — это инвариант-нарушение, пусть падает громко.
    """
    import tomli  # noqa: PLC0415

    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    with p.open("rb") as f:
        return tomli.load(f)
