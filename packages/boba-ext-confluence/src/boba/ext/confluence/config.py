"""Конфигурация confluence-extension."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.patterns import StrId
from boba.validators import ChainConverter, Default, ParseBool, ParseCsvList

__all__ = ["ConfluenceExtConfig", "ConfluenceSection"]


@dataclass(frozen=True)
class ConfluenceExtConfig:
    enable: bool = False
    tools_allow: list[str] = field(default_factory=list)


class ConfluenceSection(ConfigSection[ConfluenceExtConfig]):
    """Секция конфига confluence-extension."""

    id: ClassVar[StrId] = StrId("ext.confluence")
    namespace: ClassVar[tuple[str, ...]] = ("ext", "confluence")

    schema: ClassVar[ObjectSchema[ConfluenceExtConfig]] = ObjectSchema(
        description=(
            "Confluence-export tools (outline + section). "
            "Включаются явно через enable=true."
        ),
        fields=[
            FieldSpec(
                name="enable",
                converter=ChainConverter(Default(False), ParseBool()),
                description="Подключить Confluence-tools.",
            ),
            FieldSpec(
                name="tools_allow",
                converter=ParseCsvList(),
                description=(
                    "Whitelist по именам tools (confluence_outline, "
                    "confluence_section). Пусто — регистрируются все."
                ),
            ),
        ],
        factory=ConfluenceExtConfig,
    )
