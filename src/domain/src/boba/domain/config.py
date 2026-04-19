"""Конфигурация приложения.

:class:`AppConfig` — кросс-слойные настройки приложения + :class:`LLMConfig`.
:class:`~boba.domain.agent.models.AgentConfig` живёт в agent-слое и
загружается :class:`~boba.infra.config.ConfigLoader`-ом отдельно —
чтобы корневой ``AppConfig`` не тянул зависимость на ``agent/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LLMConfig:
    """Конфигурация LLM-клиента."""

    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    model: str = "qwen3:8b"


@dataclass(frozen=True)
class AppConfig:
    """Кросс-слойные настройки приложения.

    :class:`~boba.domain.agent.models.AgentConfig` **не** агрегируется сюда —
    он загружается инфраструктурой отдельно и инжектится в DI независимо.
    """

    workspace_base_dir: str = "./workspaces"
    ssl_verify: bool = False
    log_level: str = "INFO"
    llm: LLMConfig = field(default_factory=LLMConfig)
