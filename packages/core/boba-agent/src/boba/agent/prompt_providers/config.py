"""DTO файлового discovery system-prompt'ов: PromptsConfig."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["PromptsConfig"]


class PromptsConfig(BaseModel):
    """Путь к директории с системными prompt'ами агента."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dir: str = Field(
        description="Корневая директория .md/.txt-файлов с system-prompt'ами.",
    )
