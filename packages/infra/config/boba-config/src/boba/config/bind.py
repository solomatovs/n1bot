"""Единственная точка перехода дерева omegaconf в pydantic-модель (bind).

Ошибки:
ConfigError — значения нет, его ссылки не разрешаются или оно не
    сходится с моделью.
"""

from __future__ import annotations

from typing import TypeVar

from omegaconf import DictConfig, ListConfig, OmegaConf
from omegaconf.errors import OmegaConfBaseException
from pydantic import BaseModel, ValidationError

from boba.config.loader import ConfigError

__all__ = ["bind"]

M = TypeVar("M", bound=BaseModel)


def bind(config: DictConfig, path: str, model: type[M]) -> M:
    """Секция path из config -> провалидированная model; нет секции — ConfigError."""
    try:
        node = OmegaConf.select(config, path)
        if node is None:
            msg = (
                f"config: section [{path}] is missing, {model.__name__} expects it; "
                "add the section to the config file"
            )
            raise ConfigError(msg)

        data = node
        if isinstance(node, DictConfig | ListConfig):
            data = OmegaConf.to_container(node, resolve=True)
    except OmegaConfBaseException as exc:
        msg = f"config: resolving section [{path}] for {model.__name__} failed: {exc}"
        raise ConfigError(msg) from exc

    try:
        return model.model_validate(data)
    except ValidationError as exc:
        msg = f"config: section [{path}] does not fit {model.__name__}: {exc}"
        raise ConfigError(msg) from exc
