"""Конфигурация приложения.

:class:`AppConfig` — кросс-слойные настройки приложения + :class:`LLMConfig`.
:class:`~boba.domain.agent.models.AgentConfig` живёт в agent-слое и
загружается :class:`~boba.infra.config.ConfigLoader`-ом отдельно —
чтобы корневой ``AppConfig`` не тянул зависимость на ``agent/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LLMConfig:
    """Конфигурация LLM-клиента.

    Только транспорт (``base_url`` и ``api_key``). Имя модели — не
    часть конфига: его задаёт caller каждого запроса (UI/CLI), чтобы
    системный дефолт не просачивался в агентский луп.
    """

    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"


@dataclass(frozen=True)
class WorkspaceLayout:
    """Раскладка namespace'ов workspace'а относительно ``base_dir``.

    Поля — подкаталоги для user/system/tmp. Дискриминация делается в
    DI через маркерные сервисы, поэтому конфиг держит имена явно, а не
    через словарь по ``kind``.
    """

    base_dir: str = "./workspaces"
    user_subdir: str = "user"
    system_subdir: str = "system"
    tmp_subdir: str = "tmp"

    def root(self) -> Path:
        return Path(self.base_dir)


@dataclass(frozen=True)
class AppConfig:
    """Кросс-слойные настройки приложения.

    :class:`~boba.domain.agent.models.AgentConfig` **не** агрегируется сюда —
    он загружается инфраструктурой отдельно и инжектится в DI независимо.
    """

    workspaces: WorkspaceLayout = field(default_factory=WorkspaceLayout)
    ssl_verify: bool = False
    log_level: str = "INFO"
    # Если задан — логи пишутся в этот файл. Если ``None`` — в stdout.
    # Путь относительный резолвится от CWD процесса.
    log_file: str | None = None
    llm: LLMConfig = field(default_factory=LLMConfig)
