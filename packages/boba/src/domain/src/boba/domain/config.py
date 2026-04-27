"""Конфигурация приложения.

AppConfig — кросс-слойные настройки приложения + LLMConfig.
AgentConfig живёт в agent-слое отдельно — чтобы корневой AppConfig не
тянул зависимость на agent/. Composition AppConfig'а из плоских
ConfigSection'ов делает consumer-bootstrap (он знает, какой набор
adapter-секций зарегистрирован в фабрике); фреймворк лишь даёт
``ConfigBundle.section(cls)`` для типизированного доступа.

Конфиги расширений сюда не попадают: extension объявляет собственную
ConfigSection, и её типизированный DTO достаётся через
``ConfigBundle.section(SectionCls)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LLMConfig:
    """Конфигурация LLM-клиента.

    Только транспорт (base_url и api_key). Имя модели — не
    часть конфига: его задаёт caller каждого запроса (UI/CLI), чтобы
    системный дефолт не просачивался в агентский луп.
    """

    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"


@dataclass(frozen=True)
class WorkspaceLayout:
    """Раскладка namespace'ов workspace'а относительно base_dir.

    Поля — подкаталоги для user/system/tmp. Дискриминация делается в
    DI через маркерные сервисы, поэтому конфиг держит имена явно, а не
    через словарь по kind.
    """

    base_dir: str = "./workspaces"
    user_subdir: str = "user"
    system_subdir: str = "system"
    tmp_subdir: str = "tmp"

    def root(self) -> Path:
        return Path(self.base_dir)


@dataclass(frozen=True)
class AppConfig:
    workspaces: WorkspaceLayout = field(default_factory=WorkspaceLayout)
    ssl_verify: bool = False
    log_level: str = "INFO"
    log_file: str | None = None
    llm: LLMConfig = field(default_factory=LLMConfig)
    prompts_dir: str = "./prompts"
