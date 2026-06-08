"""Единственная точка перехода OmegaConf -> pydantic.

bind выбирает секцию по dotted-path из переданного конфиг-инстанса, резолвит
интерполяции (${...}) нативным OmegaConf и отдаёт результат в
Model.model_validate. Конфиг-инстанс и путь приходят явными аргументами —
никакого глобала и привязки пути к модели; потребители получают готовую
pydantic-модель и про OmegaConf не знают.
"""

from __future__ import annotations

from typing import TypeVar

from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel

__all__ = ["bind"]

M = TypeVar("M", bound=BaseModel)


def bind(config: DictConfig, path: str, model: type[M]) -> M:
    """Секция path из config -> провалидированная model.

    Интерполяции (${openai.openrouter}, ...) резолвятся относительно config,
    поэтому сборка секций ссылками работает. Отсутствующая секция -> пустой dict
    -> дефолты модели (или ошибка, если есть required-поля).
    """
    node = OmegaConf.select(config, path) if path else config
    if node is None:
        return model.model_validate({})
    data = OmegaConf.to_container(node, resolve=True)
    return model.model_validate(data if isinstance(data, dict) else {})
