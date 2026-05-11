"""TOML ConfigSource для Boba (boba.config-совместимый)."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

import tomli

from boba.config.builder import ConfigBundleFluentFactory
from boba.config.path import (
    ConfigPath,
    ConfigSource,
    IndexSegment,
    NameSegment,
    Segment,
)
from boba.schema.value import (
    BoolAdapter,
    DateAdapter,
    DateTimeAdapter,
    FloatAdapter,
    IntAdapter,
    NullAdapter,
    PythonValueFactory,
    ScalarValue,
    StringAdapter,
    StringValue,
    TimeAdapter,
)

__all__ = [
    "TomlFileSource",
    "TomlSource",
    "use_toml",
]


class TomlSourceBaseHelper:
    CONFIG_PATH_ENV: Final[str] = "BOBA_CONFIG_PATH"
    TOML_FILE_SUFFIX: Final[str] = "_file"

    @staticmethod
    def resolve_path(
        path: str | os.PathLike[str] | None,
    ) -> str | os.PathLike[str] | None:
        if path is not None:
            return path

        return os.environ.get(TomlSourceBaseHelper.CONFIG_PATH_ENV)

    @staticmethod
    def load_toml(path: str | os.PathLike[str] | None) -> dict[str, Any]:
        if not path:
            return {}

        p = Path(path)
        if not p.is_file():
            return {}

        with p.open("rb") as f:
            return tomli.load(f)

    @staticmethod
    def walk(
        node: Any,
        segments: tuple[Segment, ...],
    ) -> Iterable[tuple[ConfigPath, ScalarValue]]:
        """Развернуть TOML-дерево в плоский поток (path, ConfigValue)."""
        if isinstance(node, Mapping):
            for key, value in node.items():
                if not isinstance(key, str):
                    continue

                yield from TomlSourceBaseHelper.walk(
                    value, (*segments, NameSegment(key))
                )
            return

        if isinstance(node, list):
            for i, item in enumerate(node):
                yield from TomlSourceBaseHelper.walk(item, (*segments, IndexSegment(i)))

            return

        yield (
            ConfigPath(segments),
            PythonValueFactory(
                (
                    StringAdapter(),
                    BoolAdapter(),
                    IntAdapter(),
                    FloatAdapter(),
                    NullAdapter(),
                    DateTimeAdapter(),
                    DateAdapter(),
                    TimeAdapter(),
                )
            ).from_python(node),
        )


class TomlSource(ConfigSource):
    """Плоский snapshot из TOML-файла; путь — параметр или env CONFIG_PATH_ENV."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        priority: int = 100,
        name: str | None = None,
    ) -> None:
        self._raw = TomlSourceBaseHelper.load_toml(
            TomlSourceBaseHelper.resolve_path(path)
        )
        self._priority = priority
        self._name = name or "toml"

    def name(self) -> str:
        return self._name

    def priority(self) -> int:
        return self._priority

    def load(self) -> Mapping[ConfigPath, ScalarValue]:
        return dict(TomlSourceBaseHelper.walk(self._raw, ()))

    def describe(self, path: ConfigPath) -> str:
        return f"TOML {path.render()} (file: ${TomlSourceBaseHelper.CONFIG_PATH_ENV})"


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
        self._raw = TomlSourceBaseHelper.load_toml(
            TomlSourceBaseHelper.resolve_path(path)
        )
        self._priority = priority
        self._name = name or "toml_file"

    def name(self) -> str:
        return self._name

    def priority(self) -> int:
        return self._priority

    def load(self) -> Mapping[ConfigPath, ScalarValue]:
        result: dict[ConfigPath, ScalarValue] = {}
        for path, value in TomlSourceBaseHelper.walk(self._raw, ()):
            key = path.last().mapping_key()
            if key is None or not key.endswith(TomlSourceBaseHelper.TOML_FILE_SUFFIX):
                continue

            if not isinstance(value, StringValue):
                continue

            secret_path = Path(value.text)
            if not secret_path.is_file():
                continue

            content = secret_path.read_text(encoding="utf-8").rstrip("\n")
            stripped = key[: -len(TomlSourceBaseHelper.TOML_FILE_SUFFIX)]
            new_path = path.parent().join(NameSegment(stripped))
            result[new_path] = StringValue(content)
        return result

    def describe(self, path: ConfigPath) -> str:
        leaf = path.last().mapping_key() if path else None
        leaf_repr = leaf if leaf is not None else "<root>"
        return (
            f"TOML {path.parent().render()}.{leaf_repr}{TomlSourceBaseHelper.TOML_FILE_SUFFIX}"  # noqa: E501
            f"=<path-to-secret-file> (file: ${TomlSourceBaseHelper.CONFIG_PATH_ENV})"
        )


def use_toml(
    factory: ConfigBundleFluentFactory,
    path: str | os.PathLike[str] | None = None,
) -> ConfigBundleFluentFactory:
    """Подключить TomlFileSource + TomlSource к ConfigBundleFluentFactory."""
    return factory.use_source(TomlFileSource(path)).use_source(TomlSource(path))
