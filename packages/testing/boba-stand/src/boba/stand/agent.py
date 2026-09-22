"""Стенд агента: секция [agent_stand] stand.toml с локальной моделью для тестов хода
с инструментами.

Ошибки:
AgentStandError — секция [agent_stand] отсутствует или неполна.
"""

from __future__ import annotations

from typing import ClassVar, Self

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from boba.config import bind
from boba.runtime.config import ConfigLocator
from boba.stand.site import StandLayers

__all__ = ["AgentStand", "AgentStandError"]


class AgentStandError(Exception):
    """Конфиг стенда агента недоступен или неполон."""


class AgentStand(BaseModel):
    """Секция [agent_stand]: локальная onnx-модель с поддержкой инструментов и
    потолок ответа."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    SECTION: ClassVar[str] = "agent_stand"

    model_dir: str
    max_tokens: int = Field(gt=0)

    @classmethod
    def load(cls) -> Self:
        path = ConfigLocator.path()
        stand_path = path.parent / StandLayers.FILE
        if not stand_path.is_file():
            raise AgentStandError(f"agent stand: {stand_path} not found")

        raw = StandLayers.compose(path)

        try:
            return bind(raw, path=cls.SECTION, model=cls)
        except ValidationError as exc:
            raise AgentStandError(
                f"agent stand: [{cls.SECTION}] in {stand_path}: {exc}"
            ) from exc

    @classmethod
    def required(cls) -> Self:
        try:
            return cls.load()
        except AgentStandError as exc:
            pytest.skip(str(exc), allow_module_level=True)
