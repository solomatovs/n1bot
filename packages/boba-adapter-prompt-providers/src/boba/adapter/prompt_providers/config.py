"""Конфиг-секция файлового discovery system-prompt'ов."""

from __future__ import annotations

from typing import ClassVar

from boba.coercion import ChainCoercer, ParseString
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.patterns import StrId


class PromptsSection(ConfigSection[str]):
    """Путь к директории с системными prompt'ами."""

    id: ClassVar[StrId] = StrId("prompts")
    namespace: ClassVar[tuple[str, ...]] = ("prompts",)

    schema: ClassVar[ObjectSchema[str]] = ObjectSchema(
        description="Путь к директории с системными prompt'ами агента.",
        fields=[
            FieldSpec(
                name="dir",
                coercer=ChainCoercer(ParseString()),
                description="Корневая директория .md/.txt-файлов с system-prompt'ами.",
                required=True,
            ),
        ],
        factory=lambda **kw: kw["dir"],
    )
