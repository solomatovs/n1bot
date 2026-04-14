"""Конфигурация агентного цикла."""

from __future__ import annotations

from dataclasses import dataclass

from boba_domain import toml_config


@dataclass(frozen=True)
class AgentRequest:
    """Входные данные для AgentLoop.run()."""

    query: str
    model: str
    max_tokens: int


@dataclass(frozen=True)
class AgentConfig:
    """Настройки AgentLoop."""

    max_iterations: int = 10
    default_model: str = ""
    limit_message: str = (
        "Достигнут лимит итераций агента. Попробуйте переформулировать вопрос."
    )

    @staticmethod
    def from_env() -> AgentConfig:
        toml = toml_config.load_section("agent")
        return AgentConfig(
            max_iterations=int(
                toml_config.resolve(
                    "AGENT_MAX_ITERATIONS", toml, "max_iterations", "10"
                )
            ),
            default_model=toml_config.resolve(
                "AGENT_DEFAULT_MODEL", toml, "default_model", ""
            ),
            limit_message=toml_config.resolve(
                "AGENT_LIMIT_MESSAGE",
                toml,
                "limit_message",
                "Достигнут лимит итераций агента. Попробуйте переформулировать вопрос.",
            ),
        )
