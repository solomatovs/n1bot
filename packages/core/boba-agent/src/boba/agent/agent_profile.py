"""AgentProfile — переиспользуемый профиль агента (LLM + workspace + промпт).

Профиль — самостоятельная именованная сущность: набор настроек агента,
не привязанный к конкретному приложению. Приложения (cli.agent/chainlit)
подключают нужный профиль ссылкой profile = "${agent.<name>}" — как
postgres.<name> или openai.<profile>. Профилей может быть несколько
(agent.default, agent.local, ...), приложение выбирает.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from boba.provider.openai import OpenAIConfig

__all__ = ["AgentProfile"]


class AgentProfile(BaseModel):
    """Профиль агента: логи + workspace'ы + LLM-провайдер + промпт + модель."""

    model_config = ConfigDict(extra="forbid")

    log_level: str = Field(
        default="INFO",
        description="Уровень корневого логгера.",
    )
    log_file: str | None = Field(
        default=None,
        description="Путь к log-файлу. Пусто — логи в stderr.",
    )
    user_workspace_dir: str = Field(
        default="./workspaces/user",
        description=(
            "Корневая директория user-workspace'а: project-файлы, "
            "доступные tools (cat/grep/write)."
        ),
    )
    system_workspace_dir: str = Field(
        default="./workspaces/system",
        description=(
            "Корневая директория system-workspace'а: history, "
            "debug-артефакты, curl-трейсы LLM-вызовов."
        ),
    )
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    system_prompt_dir: str = Field(
        description="Корневая директория .md/.txt-файлов с system-prompt.",
    )
    model: str = Field(
        description="LLM-модель по умолчанию (напр. qwen3.5-35b). Обязательно.",
    )
    stream: bool = Field(
        default=True,
        description=(
            "Режим ответа LLM: True — стриминг дельт, False — один итоговый "
            "ответ без дельт."
        ),
    )
    max_messages: int = Field(
        default=50,
        ge=1,
        description=(
            "Размер скользящего окна диалога для CompactHistoryDialogView. "
            "Сжимает прошлые request_id до user+финальный текст-ответ "
            "и оставляет последние N сообщений в LLMRequest. "
            "Подходящее значение зависит от длины ответов модели — "
            "50 покрывает ~25 ходов чата с компактным контекстом."
        ),
    )

    extra: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Сырые provider-специфичные параметры запроса (temperature, top_p, "
            "top_k, min_p, repetition_penalty, reasoning, think и т.д.). Уходят "
            "в тело запроса как есть. Оператор сам отвечает за имена и за то, "
            "поддерживает ли их провайдер. Пусто — ничего не передаётся."
        ),
    )
    diagnostic: bool = Field(
        default=False,
        description=(
            "Показывать DiagnosticEvent-события (LLM-timings, resolved tool "
            "args, телеметрия). По умолчанию выкл."
        ),
    )

