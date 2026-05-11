"""DTO boba-cli-agent: AgentRunConfig."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from boba.llm.models import SamplingParams
from boba.schema.coercion import (
    ParseCsvList,
    ParseFloat,
    ParseInt,
    ParseString,
)

__all__ = ["AgentRunConfig"]


@dataclass(frozen=True)
class AgentRunConfig:
    """Параметры одного запуска CLI-агента: model + sampling."""

    model: Annotated[
        str,
        "LLM-модель (напр. qwen3.5-35b). Обязательно.",
        ParseString(),
    ]
    query: Annotated[
        str | None,
        "Запрос к агенту; если не задан — запускается REPL.",
        ParseString(),
    ] = None
    temperature: Annotated[
        float | None,
        "Температура sampling'а (0.0–2.0).",
        ParseFloat(),
    ] = None
    top_p: Annotated[
        float | None,
        "Nucleus sampling threshold (0.0–1.0).",
        ParseFloat(),
    ] = None
    max_tokens: Annotated[
        int | None,
        "Максимум токенов в ответе.",
        ParseInt(),
    ] = None
    seed: Annotated[
        int | None,
        "Seed для детерминистичного sampling'а.",
        ParseInt(),
    ] = None
    stop: Annotated[
        list[str] | None,
        "Stop-последовательности (CSV в env, TOML-array).",
        ParseCsvList(),
    ] = None
    frequency_penalty: Annotated[
        float | None,
        "Frequency penalty (-2.0–2.0).",
        ParseFloat(),
    ] = None
    presence_penalty: Annotated[
        float | None,
        "Presence penalty (-2.0–2.0).",
        ParseFloat(),
    ] = None

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
