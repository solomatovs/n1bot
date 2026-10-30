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

    def called_by(self, value: str) -> bool:
        """Значение ключа — вызов этого резолвера целиком."""
        return value.startswith(f"${{{self.value}:")


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

    Создают его вход программы (по путям --config) и стенды тестов (по своему
    наложению). Резолверы ${toml:…}/${toml_dir:…} регистрируются на время
    загрузки и возвращают узлы с родителем в дереве, поэтому ссылки внутри
    подключённых файлов разрешаются от корня конфига. Подключённые файлы
    читаются один раз при загрузке и ложатся в дерево таблицами: следующая
    загрузка другого конфига в том же процессе их не перечитает. Всё дерево
    разрешается сразу: неразрешимая ссылка или переменная окружения роняет
    старт, а не первый bind. Готовое дерево — только для чтения, ссылки между
    секциями в нём остаются ссылками.
    """

    def __init__(self, paths: Sequence[Path]) -> None:
        if not paths:
            msg = "config: expected at least one config file, got none"
            raise ConfigError(msg)

        self._paths = tuple(paths)
        self._base = paths[0].resolve().parent
        self._files = TomlFiles()

    def load(self) -> DictConfig:
        OmegaConf.register_new_resolver(ConfigResolver.TOML.value, self._toml, replace=True)
        OmegaConf.register_new_resolver(
            ConfigResolver.TOML_DIR.value, self._toml_dir, replace=True
        )
        layers = list(self._layers())

        try:
            merged = OmegaConf.merge(*layers)
        except OmegaConfBaseException as exc:
            msg = f"config {self._named()}: merging the files failed: {exc}"
            raise ConfigError(msg) from exc

        if not isinstance(merged, DictConfig):
            got = type(merged).__name__
            msg = f"config {self._named()}: expected the files to merge into a table, got {got}"
            raise ConfigError(msg)

        try:
            self._include(merged)
            OmegaConf.to_container(merged, resolve=True)
        except OmegaConfBaseException as exc:
            msg = f"config {self._named()}: resolving the config failed: {exc}"
            raise ConfigError(msg) from exc

        OmegaConf.set_readonly(merged, True)

        return merged

    def _layers(self) -> Iterator[DictConfig]:
        for path in self._paths:
            yield OmegaConf.create(self._files.read(path))

    def _include(self, node: DictConfig) -> None:
        """Ключи-вызовы резолверов файлов заменяются прочитанными таблицами."""
        for key, value in list(node.items_ex(resolve=False)):
            if not isinstance(value, str):
                continue

            if not self._reads_file(value):
                continue

            node[key] = node[key]

        for _, value in node.items_ex(resolve=False):
            if isinstance(value, DictConfig):
                self._include(value)

    def _reads_file(self, value: str) -> bool:
        for resolver in ConfigResolver:
            if resolver.called_by(value):
                return True

        return False

    def _toml(self, path: str, _parent_: BaseContainer) -> DictConfig:
        return OmegaConf.create(self._files.read(self._base / path), parent=_parent_)

    def _toml_dir(self, path: str, _parent_: BaseContainer) -> DictConfig:
        return OmegaConf.create(self._files.read_dir(self._base / path), parent=_parent_)

    def _named(self) -> str:
        names: list[str] = []
        for path in self._paths:
            names.append(str(path))

        return " + ".join(names)
