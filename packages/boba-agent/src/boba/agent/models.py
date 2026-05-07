"""Модели агент-слоя."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, MinValue, ParseInt
from boba.declaration import FieldSpec, ObjectSchema
from boba.llm.models import RequestId, SamplingParams


@dataclass(frozen=True)
class AgentRequest:
    """Параметры одного прогона агента (model, request_id, sampling)."""

    model: str
    request_id: RequestId
    sampling: SamplingParams | None = None


@dataclass(frozen=True)
class AgentConfig:
    """Настройки одного прогона агента."""

    max_iterations: int = 20
    max_consecutive_tool_calls: int = 3

    SCHEMA: ClassVar[ObjectSchema[AgentConfig]]


AgentConfig.SCHEMA = ObjectSchema(
    description="Лимиты агентского лупа.",
    fields=[
        FieldSpec(
            name="max_iterations",
            coercer=ChainCoercer(Default(20), ParseInt(), MinValue(1)),
            description="Жёсткий потолок числа итераций агента в одной сессии.",
        ),
        FieldSpec(
            name="max_consecutive_tool_calls",
            coercer=ChainCoercer(Default(3), ParseInt(), MinValue(1)),
            description=(
                "Сколько раз подряд агент может звать tools без LLM-ответа."
            ),
        ),
    ],
    factory=AgentConfig,
)


@dataclass
class AgentContext:
    """Mutable контекст одного прогона; iteration 1-based, 0 — до старта."""

    agent_request: AgentRequest
    config: AgentConfig = field(default_factory=AgentConfig)
    iteration: int = 0
