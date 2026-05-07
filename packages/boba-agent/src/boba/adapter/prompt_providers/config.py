"""DTO файлового discovery system-prompt'ов: PromptsConfig + SCHEMA."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import ChainCoercer, ParseString
from boba.declaration import FieldSpec, ObjectSchema

__all__ = ["PromptsConfig"]


@dataclass(frozen=True)
class PromptsConfig:
    """Путь к директории с системными prompt'ами агента."""

    dir: str

    SCHEMA: ClassVar[ObjectSchema[PromptsConfig]]


PromptsConfig.SCHEMA = ObjectSchema(
    description="Путь к директории с системными prompt'ами агента.",
    fields=[
        FieldSpec(
            name="dir",
            coercer=ChainCoercer(ParseString()),
            description="Корневая директория .md/.txt-файлов с system-prompt'ами.",
            required=True,
        ),
    ],
    factory=PromptsConfig,
)
