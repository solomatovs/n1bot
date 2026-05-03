"""Конфиг boba-cli-agent-run как ConfigSection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba_next.declaration import FieldSpec, ObjectSchema
from boba_next.config import ConfigSection
from boba_next.validators import (
    ChainConverter,
    Nullable,
    ParseCsvList,
    ParseFloat,
    ParseInt,
    ParseString,
)
from boba_next.llm.models import SamplingParams
from boba.patterns import StrId

__all__ = ["AgentRunConfig", "AgentRunSection"]


@dataclass(frozen=True)
class AgentRunConfig:
    """Параметры одного запуска boba-cli-agent-run; query=None → REPL."""

    query: str | None
    model: str
    temperature: float | None
    top_p: float | None
    max_tokens: int | None
    seed: int | None
    stop: list[str] | None
    frequency_penalty: float | None
    presence_penalty: float | None

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


class AgentRunSection(ConfigSection[AgentRunConfig]):
    """Секция agent_run."""

    id: ClassVar[StrId] = StrId("agent_run")
    namespace: ClassVar[tuple[str, ...]] = ("agent_run",)

    schema: ClassVar[ObjectSchema[AgentRunConfig]] = ObjectSchema(
        description="Параметры одного запуска CLI-агента: model + sampling.",
        fields=[
            FieldSpec(
                name="query",
                converter=Nullable(ParseString()),
                description="Запрос к агенту; если не задан — запускается REPL.",
            ),
            FieldSpec(
                name="model",
                converter=ChainConverter(ParseString()),
                description="LLM-модель (напр. qwen3.5-35b). Обязательно.",
                required=True,
            ),
            FieldSpec(
                name="temperature",
                converter=Nullable(ParseFloat()),
                description="Температура sampling'а (0.0–2.0).",
            ),
            FieldSpec(
                name="top_p",
                converter=Nullable(ParseFloat()),
                description="Nucleus sampling threshold (0.0–1.0).",
            ),
            FieldSpec(
                name="max_tokens",
                converter=Nullable(ParseInt()),
                description="Максимум токенов в ответе.",
            ),
            FieldSpec(
                name="seed",
                converter=Nullable(ParseInt()),
                description="Seed для детерминистичного sampling'а.",
            ),
            FieldSpec(
                name="stop",
                converter=Nullable(ParseCsvList()),
                description="Stop-последовательности (CSV в env, TOML-array).",
            ),
            FieldSpec(
                name="frequency_penalty",
                converter=Nullable(ParseFloat()),
                description="Frequency penalty (-2.0–2.0).",
            ),
            FieldSpec(
                name="presence_penalty",
                converter=Nullable(ParseFloat()),
                description="Presence penalty (-2.0–2.0).",
            ),
        ],
        factory=AgentRunConfig,
    )
