"""DTO файлового discovery system-prompt'ов: PromptsConfig + SCHEMA."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.schema.coercion import ChainCoercer, ParseString, Required
from boba.schema.declaration import FieldSpec, ObjectSchema

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
            coercer=ChainCoercer(Required(), ParseString()),
            description="Корневая директория .md/.txt-файлов с system-prompt'ами.",
        ),
    ],
    factory=PromptsConfig,
)
