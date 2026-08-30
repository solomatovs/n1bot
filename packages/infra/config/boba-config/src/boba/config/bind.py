"""Единственная точка перехода OmegaConf -> pydantic (bind)."""

from __future__ import annotations

from typing import TypeVar

from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel

__all__ = ["bind"]

M = TypeVar("M", bound=BaseModel)


def bind(config: DictConfig, path: str, model: type[M]) -> M:
    """Секция path из config -> провалидированная model; нет секции -> дефолты."""
    node = OmegaConf.select(config, path) if path else config
    if node is None:
        return model.model_validate({})
    data = OmegaConf.to_container(node, resolve=True)
    return model.model_validate(data if isinstance(data, dict) else {})
