"""TOML :class:`ConfigSource`-реализации.

Алгоритм мапинга :class:`ConfigKey` → TOML-путь:

    section_path = key.parts[:-1]     # вложенные таблицы
    leaf_key     = key.parts[-1]      # имя поля в листовой таблице

Например, ``ConfigKey("ext","chromadb","persist_path")`` →
``[ext.chromadb] persist_path``. Те же 2-частные ключи (``ConfigKey("llm",
"base_url")``) ложатся как ``[llm] base_url``.

:class:`TomlFileSource` — TOML-вариант Docker-style секрета: значение —
не само поле, а путь к файлу под ключом ``{leaf}_file``, содержимое
читается и возвращается с обрезанным trailing-whitespace.

TOML-данные читаются один раз при старте через :func:`load_toml`;
обычно путь хранится в env-переменной :data:`CONFIG_PATH_ENV`.
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

Конкретно эта env читается оператором bootstrap'а (не самим
:class:`TomlSource`) — источник работает с уже распарсенными данными.
"""

TOML_FILE_SUFFIX: Final[str] = "_file"
"""Суффикс leaf-ключа в TOML для секрет-указателя на файл."""


def toml_path(key: ConfigKey) -> tuple[tuple[str, ...], str]:
    """``ConfigKey`` → ``(section_path, leaf_key)`` для TOML-навигации.

    Чистая функция, доступна публично — пригодится для генерации
    operator-доки и сообщений об ошибках («задайте в TOML под
    [{'.'.join(section)}] {leaf}»).
    """
    return key.parts[:-1], key.parts[-1]


def _toml_lookup(
    data: Mapping[str, Any], section_path: Sequence[str]
) -> Mapping[str, Any] | None:
    """Спускается по вложенным TOML-таблицам; возвращает ``None`` если
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


class TomlSource(ConfigSource):
    """Значение из заранее распарсенных TOML-данных по пути из
    :class:`ConfigKey`.

    Любые типы из TOML проходят как есть (``int``/``str``/``bool``/
    ``list``/...) — конвертер ``FieldSpec`` сам их разберёт.
    """

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = data

    def resolve(self, key: ConfigKey) -> object | None:
        section_path, leaf = toml_path(key)
        section = _toml_lookup(self._data, section_path)
        if section is None:
            return None
        return section.get(leaf)


class TomlFileSource(ConfigSource):
    """Значение из файла, путь к которому хранит TOML-ключ
    ``{leaf}_file`` в той же секции, что и :class:`TomlSource` для
    ``leaf``.

    Если ключ отсутствует или файл не существует — ``None``
    (последующие источники продолжают). Содержимое возвращается с
    обрезанным trailing-whitespace.
    """

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = data

    def resolve(self, key: ConfigKey) -> object | None:
        section_path, leaf = toml_path(key)
        section = _toml_lookup(self._data, section_path)
        if section is None:
            return None
        path = section.get(leaf + TOML_FILE_SUFFIX)
        if not isinstance(path, str):
            return None
        p = Path(path)
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
