"""Секция файла конфига в pydantic-модель: один вход для утилит с --config.

Утилита получает путь к файлу и знает имя своей секции; всё остальное (чтение
toml, подключённый файл, интерполяции, переход в модель) делает этот модуль,
поэтому у утилиты нет своего разбора конфига. Как и у приложения (AppLayers), в
конфиг подкладывается вычисленный `env.base` — каталог, в котором лежит файл, —
чтобы пути в toml писались от него (`"${env.base}/krb"`), а не абсолютно.

Ошибки:
ConfigError — файла нет, он не разбирается, подключённый файл не найден или
    сам что-то подключает, секции нет или её поля не сходятся с моделью.
"""

from __future__ import annotations

import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel, ValidationError

from boba.config.bind import bind
from boba.config.builder import ConfigBuilder

__all__ = [
    "ConfigBase",
    "ConfigError",
    "TomlFile",
    "TomlKey",
    "bind_optional_section",
    "bind_section",
]

M = TypeVar("M", bound=BaseModel)


class ConfigBase:
    """Вычисленный слой конфига утилиты: секция [env] с базовым каталогом."""

    SECTION: ClassVar[str] = "env"
    BASE: ClassVar[str] = "base"

    @classmethod
    def of(cls, path: Path) -> dict[str, dict[str, str]]:
        base = path.resolve().parent
        return {cls.SECTION: {cls.BASE: str(base)}}


class ConfigError(Exception):
    """Конфиг недоступен, не разбирается или не сходится с моделью."""


class TomlKey(StrEnum):
    """Ключи верхнего уровня toml-файла конфига, которые не секции."""

    INCLUDE = "include"


class TomlFile:
    """Toml-файл конфига вместе с файлом, который он подключает.

    Ключ верхнего уровня include называет другой toml-файл путём от каталога
    этого файла; подключённый ложится слоем под ним, и сам файл перекрывает
    его ключи. Так site-файлы отладки чата, сервиса и индексаторов берут общий
    [site] из одного файла среды разработчика. Подключённый файл сам ничего не
    подключает: цепочка в один шаг. Читают его AppLayers (site-файл
    приложения), compose_file (конфиг утилиты) и стенды тестов.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def sections(self) -> dict[str, Any]:
        """Секции файла поверх секций подключённого; интерполяции не развёрнуты."""
        own = self._load(self._path)
        include = own.pop(TomlKey.INCLUDE, None)
        if include is None:
            return own

        if not isinstance(include, str):
            msg = (
                f"config {self._path}: {TomlKey.INCLUDE} expects a path of a toml "
                f"file relative to {self._path.parent}, got {include!r}"
            )
            raise ConfigError(msg)

        included_path = self._path.parent / include
        included = self._load(included_path)
        if TomlKey.INCLUDE in included:
            msg = (
                f"config {self._path}: the included file {included_path} has its "
                f"own {TomlKey.INCLUDE}; an included file includes nothing"
            )
            raise ConfigError(msg)

        builder = ConfigBuilder()
        builder.add_dict(included)
        builder.add_dict(own)
        container = OmegaConf.to_container(builder.build(), resolve=False)
        if not isinstance(container, dict):
            got = type(container).__name__
            msg = (
                f"config {self._path}: merging {included_path} gave {got}, not a table"
            )
            raise ConfigError(msg)

        merged = {str(key): value for key, value in container.items()}
        return merged

    def _load(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            msg = f"config {path}: expected a readable toml file, it does not exist"
            raise ConfigError(msg)

        try:
            with path.open("rb") as body:
                return tomllib.load(body)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            msg = f"config {path}: reading toml failed: {type(exc).__name__}: {exc}"
            raise ConfigError(msg) from exc


def compose_file(path: Path) -> DictConfig:
    """Файл конфига с вычисленным слоем env; интерполяции разворачиваются при bind."""
    sections = TomlFile(path).sections()

    try:
        builder = ConfigBuilder()
        builder.add_dict(ConfigBase.of(path))
        builder.add_dict(sections)
        return builder.build()
    except Exception as exc:
        msg = f"config {path}: building the config failed: {type(exc).__name__}: {exc}"
        raise ConfigError(msg) from exc


def bind_section(path: Path, section: str, model: type[M]) -> M:
    """Секция section файла path в модель model; интерполяции уже развёрнуты."""
    raw = compose_file(path)
    if OmegaConf.select(raw, section) is None:
        msg = f"config {path}: section [{section}] is missing"
        raise ConfigError(msg)

    try:
        return bind(raw, path=section, model=model)
    except ValidationError as exc:
        msg = f"config {path}: section [{section}] does not fit {model.__name__}: {exc}"
        raise ConfigError(msg) from exc


def bind_optional_section(path: Path, section: str, model: type[M]) -> M | None:
    """Секция, которой в файле может не быть: нет — None, есть — модель или ошибка."""
    raw = compose_file(path)
    if OmegaConf.select(raw, section) is None:
        return None

    try:
        return bind(raw, path=section, model=model)
    except ValidationError as exc:
        msg = f"config {path}: section [{section}] does not fit {model.__name__}: {exc}"
        raise ConfigError(msg) from exc
