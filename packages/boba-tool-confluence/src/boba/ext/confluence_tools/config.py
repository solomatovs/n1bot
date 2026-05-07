"""Конфигурация confluence-extension."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, ParseBool, ParseCsvList
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema

__all__ = ["ConfluenceExtConfig", "ConfluenceSection"]


@dataclass(frozen=True)
class ConfluenceExtConfig:
    enable: bool = False
    tools_allow: list[str] = field(default_factory=list)


class ConfluenceSection(ConfigSection[ConfluenceExtConfig]):
    """Секция конфига confluence-extension."""

    namespace: ClassVar[tuple[str, ...]] = ("ext", "confluence")

    schema: ClassVar[ObjectSchema[ConfluenceExtConfig]] = ObjectSchema(
        description=(
            "Online-tools для Confluence (search, page_outline, page_section). "
            "Включаются явно через enable=true."
        ),
        fields=[
            FieldSpec(
                name="enable",
                coercer=ChainCoercer(Default(False), ParseBool()),
                description="Подключить Confluence-tools.",
            ),
            FieldSpec(
                name="tools_allow",
                coercer=ParseCsvList(),
                description=(
                    "Whitelist по именам tools (confluence_search, "
                    "confluence_page_outline, confluence_page_section). "
                    "Пусто — регистрируются все."
                ),
            ),
        ],
        factory=ConfluenceExtConfig,
    )
