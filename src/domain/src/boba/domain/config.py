"""Конфигурация приложения"""

from __future__ import annotations

from dataclasses import dataclass

from boba.domain.agent.models import AgentConfig


@dataclass(frozen=True)
class LLMConfig:
    """Конфигурация LLM-клиента."""

    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    model: str = "qwen3:8b"


@dataclass(frozen=True)
class AppConfig:
    """Единственный источник конфигурации приложения."""

    workspace_base_dir: str = "./workspaces"
    ssl_verify: bool = False
    log_level: str = "INFO"
    llm: LLMConfig = LLMConfig()
    agent: AgentConfig = AgentConfig()
