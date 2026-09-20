"""Секция файла конфига в pydantic-модель: один вход для утилит с --config.

Утилита получает путь к файлу и знает имя своей секции; всё остальное (чтение
toml, интерполяции, переход в модель) делает этот модуль, поэтому у утилиты нет
своего разбора конфига.

Ошибки:
ConfigError — файла нет, он не разбирается, секции нет или её поля не
    сходятся с моделью.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from omegaconf import OmegaConf
from pydantic import BaseModel, ValidationError

from boba.config.bind import bind
from boba.config.builder import ConfigBuilder

__all__ = ["ConfigError", "bind_section"]

M = TypeVar("M", bound=BaseModel)


class ConfigError(Exception):
    """Конфиг недоступен, не разбирается или не сходится с моделью."""


def bind_section(path: Path, section: str, model: type[M]) -> M:
    """Секция section файла path в модель model; интерполяции уже развёрнуты."""
    if not path.is_file():
        msg = f"config {path}: expected a readable toml file, it does not exist"
        raise ConfigError(msg)

    try:
        raw = ConfigBuilder().add_toml(path).build()
    except Exception as exc:
        msg = f"config {path}: reading toml failed: {type(exc).__name__}: {exc}"
        raise ConfigError(msg) from exc

    if OmegaConf.select(raw, section) is None:
        msg = f"config {path}: section [{section}] is missing"
        raise ConfigError(msg)

    try:
        return bind(raw, path=section, model=model)
    except ValidationError as exc:
        msg = f"config {path}: section [{section}] does not fit {model.__name__}: {exc}"
        raise ConfigError(msg) from exc
