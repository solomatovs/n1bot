"""DTO boba-cli-agent: AgentRunConfig + SCHEMA."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.llm.models import SamplingParams
from boba.schema.coercion import (
    ChainCoercer,
    Nullable,
    ParseCsvList,
    ParseFloat,
    ParseInt,
    ParseString,
    Required,
)
from boba.schema.declaration import FieldSpec, ObjectSchema

__all__ = ["AgentRunConfig"]


@dataclass(frozen=True)
class AgentRunConfig:
    """Параметры одного запуска boba-cli-agent; query=None → REPL."""

    query: str | None
    model: str
    temperature: float | None
    top_p: float | None
    max_tokens: int | None
    seed: int | None
    stop: list[str] | None
    frequency_penalty: float | None
    presence_penalty: float | None

    SCHEMA: ClassVar[ObjectSchema[AgentRunConfig]]

    def to_sampling_params(self) -> SamplingParams | None:
        """SamplingParams из опциональных полей; None если все None."""
        fields = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "seed": self.seed,
            "stop": tuple(self.stop) if self.stop else None,
            "frequency_penalty": self.frequency_penalty,
            "presence_penalty": self.presence_penalty,
        }
        if all(v is None for v in fields.values()):
            return None
        return SamplingParams(**fields)


AgentRunConfig.SCHEMA = ObjectSchema(
    description="Параметры одного запуска CLI-агента: model + sampling.",
    fields=[
        FieldSpec(
            name="query",
            coercer=Nullable(ParseString()),
            description="Запрос к агенту; если не задан — запускается REPL.",
        ),
        FieldSpec(
            name="model",
            coercer=ChainCoercer(Required(), ParseString()),
            description="LLM-модель (напр. qwen3.5-35b). Обязательно.",
        ),
        FieldSpec(
            name="temperature",
            coercer=Nullable(ParseFloat()),
            description="Температура sampling'а (0.0–2.0).",
        ),
        FieldSpec(
            name="top_p",
            coercer=Nullable(ParseFloat()),
            description="Nucleus sampling threshold (0.0–1.0).",
        ),
        FieldSpec(
            name="max_tokens",
            coercer=Nullable(ParseInt()),
            description="Максимум токенов в ответе.",
        ),
        FieldSpec(
            name="seed",
            coercer=Nullable(ParseInt()),
            description="Seed для детерминистичного sampling'а.",
        ),
        FieldSpec(
            name="stop",
            coercer=Nullable(ParseCsvList()),
            description="Stop-последовательности (CSV в env, TOML-array).",
        ),
        FieldSpec(
            name="frequency_penalty",
            coercer=Nullable(ParseFloat()),
            description="Frequency penalty (-2.0–2.0).",
        ),
        FieldSpec(
            name="presence_penalty",
            coercer=Nullable(ParseFloat()),
            description="Presence penalty (-2.0–2.0).",
        ),
    ],
    factory=AgentRunConfig,
)
