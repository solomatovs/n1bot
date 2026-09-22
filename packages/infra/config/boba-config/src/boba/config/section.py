"""Секция файла конфига в pydantic-модель: один вход для утилит с --config.

Утилита получает путь к файлу и знает имя своей секции; всё остальное (чтение
toml, интерполяции, переход в модель) делает этот модуль, поэтому у утилиты нет
своего разбора конфига. Как и у приложения (AppLayers), в конфиг подкладывается
вычисленный `env.base` — каталог, в котором лежит файл, — чтобы пути в toml
писались от него (`"${env.base}/krb"`), а не абсолютно.

Ошибки:
ConfigError — файла нет, он не разбирается, секции нет или её поля не
    сходятся с моделью.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, TypeVar

from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel, ValidationError

from boba.config.bind import bind
from boba.config.builder import ConfigBuilder

__all__ = ["ConfigBase", "ConfigError", "bind_optional_section", "bind_section"]

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


def compose_file(path: Path) -> DictConfig:
    """Файл конфига с вычисленным слоем env; интерполяции разворачиваются при bind."""
    if not path.is_file():
        msg = f"config {path}: expected a readable toml file, it does not exist"
        raise ConfigError(msg)

    try:
        builder = ConfigBuilder()
        builder.add_dict(ConfigBase.of(path))
        builder.add_toml(path)
        return builder.build()
    except Exception as exc:
        msg = f"config {path}: reading toml failed: {type(exc).__name__}: {exc}"
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
