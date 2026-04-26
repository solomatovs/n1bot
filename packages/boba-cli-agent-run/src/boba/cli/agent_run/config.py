"""Конфиг boba-cli-agent-run как ConfigSection.

Единая секция per-CLI: описывает один запуск (model + sampling-params).
query — это вход, не конфиг; передаётся отдельно как позиционный
argparse-аргумент.

DTO имена == TOML/env-имена: [agent_run] model ↔
BOBA_AGENT_RUN_MODEL ↔ AgentRunConfig.model. Никаких
operator-side маппингов.

CLI-флаги (--model, --temperature, ...) прокидываются в
ChainedConfigResolver через CliArgsSource как
highest-priority overlay — env/TOML работают как fallback'и/дефолты.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.domain.core.config import (
    ConfigSection,
    FieldSpec,
    ObjectSchema,
)
from boba.domain.core.patterns import StrId
from boba.domain.core.validators import (
    ChainConverter,
    Nullable,
    ParseCsvList,
    ParseFloat,
    ParseInt,
    ParseString,
    Required,
)
from boba.domain.llm.models import SamplingParams

__all__ = ["AgentRunConfig", "AgentRunSection"]


@dataclass(frozen=True)
class AgentRunConfig:
    """Параметры одного запуска boba-cli-agent-run.

    model — обязательно, нет смыслового дефолта (выбор модели — это
    выбор поведения).

    Sampling-параметры опциональны: None означает «провайдеру
    ничего не передаём, поведение модели — её собственный дефолт».
    """

    model: str
    temperature: float | None
    top_p: float | None
    max_tokens: int | None
    seed: int | None
    stop: list[str] | None
    frequency_penalty: float | None
    presence_penalty: float | None

    def to_sampling_params(self) -> SamplingParams | None:
        """Сборка SamplingParams из опциональных полей. None —
        если ни одно из sampling-полей не задано (агенту параметры
        вообще не передаются).
        """
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
    """Секция agent_run. Регистрируется в ConfigFactory напрямую
    в boba.cli.agent_run.cli (own-section CLI-приложения, не
    third-party extension; entry-point не нужен).
    """

    id: ClassVar[StrId] = StrId("agent_run")
    namespace: ClassVar[tuple[str, ...]] = ("agent_run",)

    schema: ClassVar[ObjectSchema[AgentRunConfig]] = ObjectSchema(
        description="Параметры одного запуска CLI-агента: model + sampling.",
        fields=[
            FieldSpec(
                name="model",
                converter=ChainConverter(Required(), ParseString()),
                description="LLM-модель (напр. qwen3.5-35b). Обязательно.",
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
