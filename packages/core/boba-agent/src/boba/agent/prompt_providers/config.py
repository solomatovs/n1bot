"""DTO файлового discovery system-prompt'ов: PromptsConfig."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from boba.schema.coercion import ParseString

__all__ = ["PromptsConfig"]


@dataclass(frozen=True)
class PromptsConfig:
    """Путь к директории с системными prompt'ами агента."""

    dir: Annotated[
        str,
        "Корневая директория .md/.txt-файлов с system-prompt'ами.",
        ParseString(),
    ]
