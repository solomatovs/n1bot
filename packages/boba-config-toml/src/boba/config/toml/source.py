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

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from boba.domain.core.config import ConfigKey, ConfigSource

__all__ = [
    "CONFIG_PATH_ENV",
    "TOML_FILE_SUFFIX",
    "TomlFileSource",
    "TomlSource",
    "load_toml",
    "toml_path",
]


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


def _resolve_path(path: str | os.PathLike[str] | None) -> str | os.PathLike[str] | None:
    """Если path не задан — берём из env CONFIG_PATH_ENV."""
    return path if path is not None else os.environ.get(CONFIG_PATH_ENV)


class TomlSource(ConfigSource):
    """Значение из TOML-файла по пути из ConfigKey.

    На старте читает файл (по умолчанию — из env CONFIG_PATH_ENV) и
    кеширует. Любые типы из TOML проходят как есть (int/str/bool/
    list/...) — конвертер FieldSpec сам их разберёт.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._data = load_toml(_resolve_path(path))

    def resolve(self, key: ConfigKey) -> object | None:
        section_path, leaf = toml_path(key)
        section = _toml_lookup(self._data, section_path)
        if section is None:
            return None
        return section.get(leaf)


class TomlFileSource(ConfigSource):
    """Значение из файла, путь к которому хранит TOML-ключ {leaf}_file
    в той же секции, что и TomlSource для leaf.

    Если ключ отсутствует или файл не существует — None (последующие
    источники продолжают). Содержимое возвращается с обрезанным
    trailing-whitespace.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._data = load_toml(_resolve_path(path))

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
