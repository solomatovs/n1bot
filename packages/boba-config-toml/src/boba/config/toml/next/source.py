"""TOML-источник под confignext: плоский snapshot из tomli."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

import tomli

from boba.domain.core.confignext.path import (
    ConfigPath,
    IndexSegment,
    NameSegment,
    Segment,
)
from boba.domain.core.confignext.source import ConfigSource
from boba.domain.core.confignext.value import (
    BoolAdapter,
    ConfigValue,
    DateAdapter,
    DateTimeAdapter,
    FloatAdapter,
    IntAdapter,
    NullAdapter,
    PythonValueFactory,
    StringAdapter,
    StringValue,
    TimeAdapter,
)

__all__ = [
    "CONFIG_PATH_ENV",
    "TOML_FILE_SUFFIX",
    "TomlFileSource",
    "TomlSource",
]


CONFIG_PATH_ENV: Final[str] = "BOBA_CONFIG_PATH"
TOML_FILE_SUFFIX: Final[str] = "_file"


class TomlSource(ConfigSource):
    """Плоский snapshot из TOML-файла; путь — параметр или env CONFIG_PATH_ENV."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        priority: int = 100,
        name: str | None = None,
    ) -> None:
        self._raw = _load_toml(_resolve_path(path))
        self._priority = priority
        self._name = name or "toml"

    def name(self) -> str:
        return self._name

    def priority(self) -> int:
        return self._priority

    def load(self) -> Mapping[ConfigPath, ConfigValue]:
        return dict(_walk(self._raw, ()))

    def describe(self, path: ConfigPath) -> str:
        return f"TOML {path.render()} (file: ${CONFIG_PATH_ENV})"


class TomlFileSource(ConfigSource):
    """Значения-секреты из файлов: leaf вида `<name>_file = "<path>"`.

    Возвращает плоский snapshot, в котором `*_file`-leaf-имена заменены на
    leaf без суффикса, а значение — содержимое файла (строкой, без trailing
    whitespace). Используется для Docker secrets.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        priority: int = 200,
        name: str | None = None,
    ) -> None:
        self._raw = _load_toml(_resolve_path(path))
        self._priority = priority
        self._name = name or "toml_file"

    def name(self) -> str:
        return self._name

    def priority(self) -> int:
        return self._priority

    def load(self) -> Mapping[ConfigPath, ConfigValue]:
        result: dict[ConfigPath, ConfigValue] = {}
        for path, value in _walk(self._raw, ()):
            key = path.last().mapping_key()
            if key is None or not key.endswith(TOML_FILE_SUFFIX):
                continue
            if not isinstance(value, StringValue):
                continue
            secret_path = Path(value.text)
            if not secret_path.is_file():
                continue
            content = secret_path.read_text(encoding="utf-8").rstrip("\n")
            stripped = key[: -len(TOML_FILE_SUFFIX)]
            new_path = path.parent().join(NameSegment(stripped))
            result[new_path] = StringValue(content)
        return result

    def describe(self, path: ConfigPath) -> str:
        leaf = path.last().mapping_key() if path else None
        leaf_repr = leaf if leaf is not None else "<root>"
        return (
            f"TOML {path.parent().render()}.{leaf_repr}{TOML_FILE_SUFFIX}"
            f"=<path-to-secret-file> (file: ${CONFIG_PATH_ENV})"
        )


def _resolve_path(
    path: str | os.PathLike[str] | None,
) -> str | os.PathLike[str] | None:
    if path is not None:
        return path
    return os.environ.get(CONFIG_PATH_ENV)


def _load_toml(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    with p.open("rb") as f:
        return tomli.load(f)


def _walk(
    node: Any,
    segments: tuple[Segment, ...],
) -> Iterable[tuple[ConfigPath, ConfigValue]]:
    """Развернуть TOML-дерево в плоский поток (path, ConfigValue)."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            if not isinstance(key, str):
                continue
            yield from _walk(value, (*segments, NameSegment(key)))
        return
    if isinstance(node, list):
        for i, item in enumerate(node):
            yield from _walk(item, (*segments, IndexSegment(i)))
        return

    adapters = (
        StringAdapter(),
        BoolAdapter(),
        IntAdapter(),
        FloatAdapter(),
        NullAdapter(),
        DateTimeAdapter(),
        DateAdapter(),
        TimeAdapter(),
    )

    yield ConfigPath(segments), PythonValueFactory(adapters).from_python(node)
