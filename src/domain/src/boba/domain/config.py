"""Конфигурация приложения.

:class:`AppConfig` — кросс-слойные настройки приложения + :class:`LLMConfig`.
:class:`~boba.domain.agent.models.AgentConfig` живёт в agent-слое и
загружается :class:`~boba.infra.config.ConfigLoader`-ом отдельно —
чтобы корневой ``AppConfig`` не тянул зависимость на ``agent/``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from boba.domain.core.workspace import (
    SYSTEM_WORKSPACE_KIND,
    TMP_WORKSPACE_KIND,
    USER_WORKSPACE_KIND,
    WorkspaceKind,
)


@dataclass(frozen=True)
class LLMConfig:
    """Конфигурация LLM-клиента."""

    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    model: str = "qwen3:8b"


@dataclass(frozen=True)
class WorkspaceLayout:
    """Раскладка namespace'ов workspace'а относительно ``base_dir``.

    Единый источник истины для путей: DI-проводка не знает, как называется
    подкаталог под user/system/tmp — только спрашивает ``resolve(kind)``.
    Дефолты = ``kind.name`` (``user`` / ``system`` / ``tmp``), но каждое
    имя можно переопределить через конфиг.
    """

    base_dir: str = "./workspaces"
    subdirs: Mapping[WorkspaceKind, str] = field(
        default_factory=lambda: MappingProxyType(
            {
                USER_WORKSPACE_KIND: USER_WORKSPACE_KIND.name,
                SYSTEM_WORKSPACE_KIND: SYSTEM_WORKSPACE_KIND.name,
                TMP_WORKSPACE_KIND: TMP_WORKSPACE_KIND.name,
            }
        )
    )

    def root(self) -> Path:
        return Path(self.base_dir)

    def subdir(self, kind: WorkspaceKind) -> str:
        try:
            return self.subdirs[kind]
        except KeyError as e:
            raise KeyError(f"no subdir configured for kind {kind!r}") from e


@dataclass(frozen=True)
class AppConfig:
    """Кросс-слойные настройки приложения.

    :class:`~boba.domain.agent.models.AgentConfig` **не** агрегируется сюда —
    он загружается инфраструктурой отдельно и инжектится в DI независимо.
    """

    workspaces: WorkspaceLayout = field(default_factory=WorkspaceLayout)
    ssl_verify: bool = False
    log_level: str = "INFO"
    llm: LLMConfig = field(default_factory=LLMConfig)
