"""Модели агент-слоя."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from boba.domain.core.patterns import Always, Specification
from boba.domain.core.tools.tool import Tool
from boba.domain.llm.models import RequestId, SamplingParams


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
    tool_spec: Specification[Tool[Any]] = field(default_factory=Always)


@dataclass
class AgentContext:
    """Mutable контекст одного прогона; iteration 1-based, 0 — до старта."""

    agent_request: AgentRequest
    config: AgentConfig = field(default_factory=AgentConfig)
    iteration: int = 0
