"""Профиль модели и пределы агента: конфиг-граница ядра.

Окно, предел ответа и возможности модели задаёт администратор (план,
раздел 5.11); отсюда считаются порог сжатия и блокирующий предел, оценка
текста в токенах без провайдера и проверка на старте: системный промпт с
описаниями инструментов должен занимать меньше половины порога сжатия.

Ошибки:
ProfileError — профиль несовместим с первой версией или промпт не помещается
    в окно так, чтобы сжатие не срабатывало на каждом ходу.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.llm.providers import ChatModelConfig

__all__ = [
    "CompactionSettings",
    "ModelProfile",
    "ProfileCheck",
    "ProfileError",
    "ReasoningReturn",
    "TurnLimits",
]


class ProfileError(Exception):
    """Профиль модели не годится для запуска."""


class ReasoningReturn(StrEnum):
    """Возвращать ли модели её рассуждения прошлых ответов."""

    NEVER = "never"
    TURN = "turn"
    ALWAYS = "always"


class ModelProfile(BaseModel):
    """Секция `[agent.model]`: обращение к порту и то, что о модели знает агент."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    RESERVE_CAP: ClassVar[int] = 20_000
    """Верхняя граница запаса под ответ в формуле действующего окна."""

    chat: ChatModelConfig
    context_window: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    chars_per_token: int = Field(default=3, gt=0)
    supports_tools: bool = True
    supports_images: bool = False
    system_turns: bool = False
    reasoning_return: ReasoningReturn = ReasoningReturn.NEVER

    @property
    def model_id(self) -> str:
        return self.chat.model

    def effective_window(self) -> int:
        """Окно без запаса под ответ: `context_window − min(max_output, 20 000)`."""
        reserve = min(self.max_output_tokens, self.RESERVE_CAP)

        return max(1, self.context_window - reserve)

    def estimate(self, text: str) -> int:
        """Оценка текста в токенах без провайдера."""
        return math.ceil(len(text) / self.chars_per_token)


class CompactionSettings(BaseModel):
    """Секция `[agent.compaction]`: доли действующего окна."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    compact_at: float = Field(default=0.8, gt=0, lt=1)
    block_at: float = Field(default=0.95, gt=0, le=1)

    def threshold(self, profile: ModelProfile) -> int:
        return int(profile.effective_window() * self.compact_at)

    def limit(self, profile: ModelProfile) -> int:
        return int(profile.effective_window() * self.block_at)


class TurnLimits(BaseModel):
    """Секция `[agent.limits]`: пределы хода и повторов."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_turns: int = Field(default=100, gt=0)
    tool_result_max_chars: int = Field(default=50_000, gt=0)
    api_retries: int = Field(default=3, ge=0)
    retry_delays_sec: Sequence[float] = (1.0, 2.0, 4.0)
    output_limit_retries: int = Field(default=3, ge=0)

    def delay_before(self, attempt: int) -> float:
        """Пауза перед повтором с номером attempt (с 1); хвост — последняя пауза."""
        if not self.retry_delays_sec:
            return 0.0

        index = min(attempt, len(self.retry_delays_sec)) - 1

        return self.retry_delays_sec[max(index, 0)]


class ProfileCheck:
    """Проверка профиля на старте: возможности и место под промпт."""

    HALF: ClassVar[int] = 2

    def __init__(self, profile: ModelProfile, compaction: CompactionSettings) -> None:
        self._profile = profile
        self._compaction = compaction

    def run(self, system_prompt: Sequence[str], tool_texts: Sequence[str]) -> None:
        if not self._profile.supports_tools:
            msg = (
                f"The model '{self._profile.model_id}' is marked supports_tools = "
                "false. The agent works through tool calls, so it cannot use this "
                "model. Pick a model with tool calls, or set supports_tools = true if "
                "it has them."
            )
            raise ProfileError(msg)

        used = 0
        for text in [*system_prompt, *tool_texts]:
            used += self._profile.estimate(text)

        allowed = self._compaction.threshold(self._profile) // self.HALF
        if used >= allowed:
            msg = (
                f"The system prompt and tool descriptions for "
                f"'{self._profile.model_id}' take about {used} tokens, but they must "
                f"stay under {allowed}: half "
                f"of the compaction threshold for context_window = "
                f"{self._profile.context_window}. Otherwise compaction would fire on "
                "every turn. Shorten the prompt or the tools, or raise context_window."
            )
            raise ProfileError(msg)
