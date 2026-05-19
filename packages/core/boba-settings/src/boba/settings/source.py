"""
ConfigSource — единый загрузчик конфигурации для агента
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

__all__ = [
    "ConfigPath",
    "ConfigSource",
    "ConfigSourcePydanticAdapter",
    "DictConfigSource",
    "TomlEnvConfigSource",
    "to_config_path",
]


ConfigPath = tuple[str, ...]
"""Канонический ключ конфиг-секции.

Пример: `("tool", "files")` ↔
    - TOML: `[tool.files]`
    - env:  `BOBA_TOOL__FILES__*`
    - dict: `("tool", "files")` или `"tool.files"`
"""


def to_config_path(key: ConfigPath | str | None) -> ConfigPath:
    """Привести ключ к каноническому `ConfigPath`.

    - None или пустая строка это ()
    - Строка через точку ("tool.files" этл ("tool","files"))
    - tuple/list это tuple (нормализуется)
    """
    if not key:
        return ()

    if isinstance(key, str):
        return tuple(s for s in key.split(".") if s)

    return tuple(key)


@runtime_checkable
class ConfigSource(Protocol):
    """Источник конфигурации — отдаёт dict для указанного path."""

    def for_path(self, path: ConfigPath) -> Mapping[str, Any]:
        """
        Получить dict для указанного пути

        Пустой path или отсутствие данных по path это пустой dict
        """
        ...


_DEFAULT_TOML_PATH_ENV = "BOBA_CONFIG_PATH"
"""Имя env-var, в котором ищется путь к TOML-файлу по умолчанию."""

_DEFAULT_ENV_PREFIX = "BOBA_"
"""Глобальный namespace env-vars по умолчанию."""

_DEFAULT_DELIMITER = "__"
"""Разделитель сегментов в env-vars по умолчанию."""


class TomlEnvConfigSource(ConfigSource):
    """
    ConfigSource на базе TOML-файла + env-vars.

    По умолчанию читает `$BOBA_CONFIG_PATH` (если задан и файл существует)
    и `os.environ`

    Трансляция ConfigPath:
        - TOML: '.'.join(path)                      -> секция [seg1.seg2]
        - env:  <env_prefix><SEG1>__<SEG2>__<KEY>   -> flat/nested-keys

    env приоритетней TOML значений

    Args:
        toml_path:
            явный путь к TOML или `None` (default) — читать из
            `$BOBA_CONFIG_PATH`
        env: маппинг env-vars. `None` (default) = `os.environ` (live).
        env_prefix: глобальный namespace env-vars. По умолчанию `"BOBA_"`.
        env_delimiter: разделитель сегментов в env. По умолчанию `"__"`.
        toml_path_env_var: имя env-var, в котором ищется путь к TOML.
    """

    def __init__(
        self,
        *,
        toml_path: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        env_prefix: str = _DEFAULT_ENV_PREFIX,
        env_delimiter: str = _DEFAULT_DELIMITER,
        toml_path_env_var: str = _DEFAULT_TOML_PATH_ENV,
    ) -> None:
        self._toml_path_explicit: Path | None = (
            Path(toml_path) if toml_path else None
        )
        # Если toml_path не задан — каждый раз спрашиваем env-var.
        self._use_env_for_toml = not toml_path
        self._env: Mapping[str, str] = env if env is not None else os.environ
        self._env_prefix = env_prefix
        self._env_delimiter = env_delimiter
        self._toml_path_env_var = toml_path_env_var

    def for_path(self, path: ConfigPath) -> Mapping[str, Any]:
        if not path:
            return {}

        result: dict[str, Any] = {}

        toml_data = self._read_toml(path)
        if toml_data:
            result.update(toml_data)

        env_data = self._read_env(path)
        if env_data:
            result.update(env_data)

        return result

    def _read_toml(self, path: ConfigPath) -> Mapping[str, Any]:
        toml_path = self._current_toml_path()
        if toml_path is None or not toml_path.is_file():
            return {}

        with toml_path.open("rb") as f:
            toml_doc = tomllib.load(f)

        for seg in path:
            if not isinstance(toml_doc, Mapping):
                return {}
            toml_doc = toml_doc.get(seg, {})

        res = toml_doc if isinstance(toml_doc, Mapping) else {}

        return res

    def _read_env(self, path: ConfigPath) -> Mapping[str, Any]:
        full_prefix = (
            self._env_prefix
            + self._env_delimiter.join(p.upper() for p in path)
            + self._env_delimiter
        )
        out: dict[str, Any] = {}
        for key, value in self._env.items():
            if not key.startswith(full_prefix):
                continue

            tail = key[len(full_prefix):]

            parts = [p.lower() for p in tail.split(self._env_delimiter) if p]
            if not parts:
                continue

            cursor: dict[str, Any] = out
            for part in parts[:-1]:
                child = cursor.get(part)
                if not isinstance(child, dict):
                    child = {}
                    cursor[part] = child
                cursor = child
            cursor[parts[-1]] = value

        return out

    def _current_toml_path(self) -> Path | None:
        if not self._use_env_for_toml:
            return self._toml_path_explicit

        val = self._env.get(self._toml_path_env_var)

        return Path(val) if val else None


class DictConfigSource(ConfigSource):
    """
    In-memory ConfigSource — для тестов и явных конфигураций.
    """

    def __init__(
        self,
        data: Mapping[ConfigPath | str, Mapping[str, Any]] | None = None,
    ) -> None:
        self._data: dict[ConfigPath, dict[str, Any]] = {}
        if data:
            for k, v in data.items():
                self._data[to_config_path(k)] = dict(v)

    def for_path(self, path: ConfigPath) -> Mapping[str, Any]:
        return dict(self._data.get(tuple(path), {}))


class ConfigSourcePydanticAdapter(PydanticBaseSettingsSource):
    """
    Мост между `ConfigSource` и `pydantic_settings.PydanticBaseSettingsSource`
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        source: ConfigSource,
        path: ConfigPath,
    ) -> None:
        super().__init__(settings_cls)
        self._source = source
        self._path = path

    def get_field_value(
        self,
        field: FieldInfo,
        field_name: str,
    ) -> tuple[Any, str, bool]:
        del field
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._source.for_path(self._path))
