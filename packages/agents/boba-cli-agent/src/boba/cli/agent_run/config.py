"""DTO boba-cli-agent: AgentRunConfig.

Источники (priority high → low):
  1. CLI argv:  --model qwen3.5-35b --temperature 0.7 --query "..." ...
  2. init-kwargs (`AgentRunConfig(model="...")`).
  3. env:       BOBA_CLI__MODEL, BOBA_CLI__TEMPERATURE, ...
  4. TOML:      секция [cli] в файле $BOBA_CONFIG_PATH.
"""

from __future__ import annotations

from pydantic import Field

from boba.llm.models import SamplingParams
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict, StringList

__all__ = ["AgentRunConfig"]


class AgentRunConfig(BobaFlatSettings):
    """Параметры одного запуска CLI-агента: model + sampling."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        boba_env_prefix="BOBA_CLI__",
        boba_toml_section="cli",
        boba_cli=True,
    )

    model: str = Field(description="LLM-модель (напр. qwen3.5-35b). Обязательно.")
    query: str | None = Field(
        default=None,
        description="Запрос к агенту; если не задан — запускается REPL.",
    )
    temperature: float | None = Field(
        default=None,
        description="Температура sampling'а (0.0–2.0).",
    )
    top_p: float | None = Field(
        default=None,
        description="Nucleus sampling threshold (0.0–1.0).",
    )
    max_tokens: int | None = Field(
        default=None,
        description="Максимум токенов в ответе.",
    )
    seed: int | None = Field(
        default=None,
        description="Seed для детерминистичного sampling'а.",
    )
    stop: StringList | None = Field(
        default=None,
        description="Stop-последовательности (CSV в env, TOML-array).",
    )
    frequency_penalty: float | None = Field(
        default=None,
        description="Frequency penalty (-2.0–2.0).",
    )
    presence_penalty: float | None = Field(
        default=None,
        description="Presence penalty (-2.0–2.0).",
    )

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
