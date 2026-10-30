"""Загрузка конфига программы: TOML-файлы в одно дерево omegaconf.

Главный файл и файлы-наложения (повторный --config) читаются tomllib и
сливаются штатным OmegaConf.merge. Файл или каталог файлов внутри конфига
подключают резолверы ${toml:<файл>} и ${toml_dir:<каталог>}; относительный
путь у них считается от каталога главного файла. Окружение конфиг берёт сам
ссылкой ${oc.env:ИМЯ}.

Ошибки:
ConfigError — файл не читается или не TOML, ссылка, файл резолвера или
    переменная окружения не разрешаются, секции нет или она не сходится с
    моделью.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from omegaconf import DictConfig, OmegaConf
from omegaconf.basecontainer import BaseContainer
from omegaconf.errors import OmegaConfBaseException

__all__ = ["ConfigError", "ConfigLoader", "ConfigResolver"]


class ConfigError(Exception):
    """Конфиг недоступен, не разбирается, не разрешается или не сходится с моделью."""


class ConfigResolver(StrEnum):
    """Резолверы, которыми конфиг подключает файлы."""

    TOML = "toml"
    TOML_DIR = "toml_dir"


class TomlFiles:
    """Чтение TOML-файла или каталога TOML-файлов в таблицу; ошибка чтения —
    ConfigError с путём."""

    SUFFIX: ClassVar[str] = ".toml"

    def read(self, path: Path) -> dict[str, Any]:
        try:
            with path.open("rb") as body:
                return tomllib.load(body)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            msg = f"config {path}: reading a toml file failed: {type(exc).__name__}: {exc}"
            raise ConfigError(msg) from exc

    def read_dir(self, path: Path) -> dict[str, Any]:
        """Файлы <имя>.toml каталога — таблица по именам файлов."""
        if not path.is_dir():
            msg = f"config {path}: expected a directory of {self.SUFFIX} files, it does not exist"
            raise ConfigError(msg)

        tables: dict[str, Any] = {}
        for file in sorted(path.glob(f"*{self.SUFFIX}")):
            tables[file.stem] = self.read(file)

        return tables


class ConfigLoader:
    """Загрузчик конфига процесса: главный файл, поверх него наложения.

    Создают его вход программы (по путям --config) и стенды тестов. Файлы
    сливаются OmegaConf.merge, резолверы ${toml:…}/${toml_dir:…}
    регистрируются на время загрузки, и всё дерево один раз разрешается
    OmegaConf.to_container(resolve=True): ссылки, переменные окружения и
    подключённые файлы читаются здесь, неразрешимое роняет старт. Программа
    получает разрешённое дерево только для чтения, ссылок в нём больше нет.
    Наложение в памяти (load_over) нужно стендам тестов: оно ложится
    последним слоем до разрешения, поэтому ссылки на изменённые ключи
    следуют за ними.
    """

    def __init__(self, paths: Sequence[Path]) -> None:
        if not paths:
            msg = "config: expected at least one config file, got none"
            raise ConfigError(msg)

        self._paths = tuple(paths)
        self._base = paths[0].resolve().parent
        self._files = TomlFiles()

    def load(self) -> DictConfig:
        return self._resolved(list(self._layers()))

    def load_over(self, overlay: DictConfig) -> DictConfig:
        """Файлы и поверх них наложение в памяти, затем разрешение."""
        layers = list(self._layers())
        layers.append(overlay)

        return self._resolved(layers)

    def _resolved(self, layers: Sequence[DictConfig]) -> DictConfig:
        OmegaConf.register_new_resolver(ConfigResolver.TOML.value, self._toml, replace=True)
        OmegaConf.register_new_resolver(
            ConfigResolver.TOML_DIR.value, self._toml_dir, replace=True
        )

        try:
            merged = OmegaConf.merge(*layers)
            resolved = OmegaConf.to_container(merged, resolve=True)
            tree = OmegaConf.create(resolved)
        except OmegaConfBaseException as exc:
            msg = f"config {self._named()}: resolving the config failed: {exc}"
            raise ConfigError(msg) from exc

        if not isinstance(tree, DictConfig):
            got = type(tree).__name__
            msg = f"config {self._named()}: expected the files to merge into a table, got {got}"
            raise ConfigError(msg)

        OmegaConf.set_readonly(tree, True)

        return tree

    def _layers(self) -> Iterator[DictConfig]:
        for path in self._paths:
            yield OmegaConf.create(self._files.read(path))

    def _toml(self, path: str, _parent_: BaseContainer) -> DictConfig:
        return OmegaConf.create(self._files.read(self._base / path), parent=_parent_)

    def _toml_dir(self, path: str, _parent_: BaseContainer) -> DictConfig:
        return OmegaConf.create(self._files.read_dir(self._base / path), parent=_parent_)

    def _named(self) -> str:
        names: list[str] = []
        for path in self._paths:
            names.append(str(path))

        return " + ".join(names)
