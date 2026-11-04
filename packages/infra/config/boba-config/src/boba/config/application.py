"""Чтение модели приложения и исходного дерева TOML.

Ошибки:
ConfigError — файлы, интерполяция или секция не соответствуют модели.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from omegaconf import DictConfig
from pydantic import BaseModel

from boba.config import ConfigLoader, ConfigSchema, bind

ConfigT = TypeVar("ConfigT", bound=BaseModel)


@dataclass(frozen=True)
class LoadedConfig(Generic[ConfigT]):
    """Проверенная модель и дерево секций для сборки одного приложения."""

    value: ConfigT
    raw: DictConfig


class ConfigFiles(Generic[ConfigT]):
    """Читает переданную приложением модель; не меняет окружение процесса."""

    def __init__(
        self,
        model: type[ConfigT],
        *,
        section: str = "app",
        values: Sequence[str] = (),
    ) -> None:
        self._model = model
        self._section = section
        self._schema = ConfigSchema(model, section, values=values)

    def read(self, paths: Sequence[Path]) -> DictConfig:
        return ConfigLoader(paths, schema=self._schema).load()

    def load(self, paths: Sequence[Path]) -> LoadedConfig[ConfigT]:
        raw = self.read(paths)
        value = bind(raw, self._section, self._model)

        return LoadedConfig(value, raw)
