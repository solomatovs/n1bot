"""Конфигурация агентного цикла."""
from __future__ import annotations

from dataclasses import dataclass

from boba_domain.config import _load_toml_section, _resolve


@dataclass(frozen=True)
class AgentConfig:
    """Настройки AgentLoop."""
    max_iterations: int = 10
    default_model: str = ""
    limit_message: str = "Достигнут лимит итераций агента. Попробуйте переформулировать вопрос."

    @staticmethod
    def from_env() -> AgentConfig:
        toml = _load_toml_section("agent")
        return AgentConfig(
            max_iterations=int(_resolve("AGENT_MAX_ITERATIONS", toml, "max_iterations", "10")),
            default_model=_resolve("AGENT_DEFAULT_MODEL", toml, "default_model", ""),
            limit_message=_resolve("AGENT_LIMIT_MESSAGE", toml, "limit_message",
                                   "Достигнут лимит итераций агента. Попробуйте переформулировать вопрос."),
        )
