"""Pydantic-конфиги инструментов прототипа (порт boba.tool.shell.config).

Резолвятся из секции плагина [tool.shell] через boba.settings.bind.
Плагин-уровневые поля enable/tools читает plugins.py, здесь extra="ignore"
позволяет им жить в общей TOML-секции.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.chainlit2.agent.tools.profile_local import DEFAULT_PASSTHROUGH

__all__ = ["BashLocalConfig"]


class BashLocalConfig(BaseModel):
    """Конфиг bash_local: subprocess без bwrap-изоляции.

    Config-секция: [tool.shell]. Все поля — operator-controlled. LLM
    выбирает только command/stdin, остальное задано здесь.
    """

    model_config = ConfigDict(extra="ignore")

    workspace_root: Path = Field(
        default=Path(),
        description=(
            "Host-путь к корню проекта. Резолвится до абсолютного. "
            "Используется как cwd по умолчанию для subprocess'а."
        ),
    )
    cwd: str = Field(
        default="",
        description=(
            "Рабочая директория. Пустая строка = workspace_root. Не "
            "валидируется на существование — Popen вернёт ошибку."
        ),
    )
    env_passthrough: tuple[str, ...] = Field(
        default=DEFAULT_PASSTHROUGH,
        description=(
            "Host-env переменные, наследуемые внутрь процесса. По умолчанию — "
            "безопасный минимум. Пустой tuple = ничего не наследовать."
        ),
    )
    env_set: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Явные env-переменные. Перекрывают env_passthrough при совпадении имён."
        ),
    )
    timeout_sec: int = Field(
        default=30,
        ge=1,
        le=3600,
        description="Жёсткий таймаут выполнения (1..3600 сек).",
    )
    max_output_bytes: int = Field(
        default=256 * 1024,
        ge=1024,
        description=(
            "Лимит stdout И stderr по отдельности (мин. 1024). Превышение -> "
            "обрезка и `truncated=True` в результате."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """workspace_root резолвится до абсолютного пути и проверяется."""
        resolved = self.workspace_root.expanduser().resolve(strict=False)
        if not resolved.exists():
            msg = f"bash_local.workspace_root не существует: {resolved}"
            raise ValueError(msg)
        if not resolved.is_dir():
            msg = f"bash_local.workspace_root не директория: {resolved}"
            raise ValueError(msg)
        object.__setattr__(self, "workspace_root", resolved)
        return self
