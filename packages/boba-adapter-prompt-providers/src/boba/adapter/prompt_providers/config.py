"""Конфиг-секция файлового discovery system-prompt'ов."""

from __future__ import annotations

from typing import ClassVar

from boba.domain.core.config import ConfigSection, FieldSpec, ObjectSchema
from boba.domain.core.patterns import StrId
from boba.domain.core.validators import ChainConverter, ParseString, Required


class PromptsSection(ConfigSection[str]):
    """Путь к директории с системными prompt'ами."""

    id: ClassVar[StrId] = StrId("prompts")
    namespace: ClassVar[tuple[str, ...]] = ("prompts",)

    schema: ClassVar[ObjectSchema[str]] = ObjectSchema(
        description="Путь к директории с системными prompt'ами агента.",
        fields=[
            FieldSpec(
                name="dir",
                converter=ChainConverter(Required(), ParseString()),
                description="Корневая директория .md/.txt-файлов "
                "с system-prompt'ами.",
            ),
        ],
        factory=lambda **kw: kw["dir"],
    )
