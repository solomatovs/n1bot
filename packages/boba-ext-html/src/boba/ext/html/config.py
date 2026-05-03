"""Конфигурация html-extension."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.patterns import StrId
from boba.validators import ChainConverter, Default, ParseBool, ParseCsvList

__all__ = ["HtmlExtConfig", "HtmlSection"]


@dataclass(frozen=True)
class HtmlExtConfig:
    enable: bool = False
    tools_allow: list[str] = field(default_factory=list)


class HtmlSection(ConfigSection[HtmlExtConfig]):
    """Секция конфига html-extension."""

    id: ClassVar[StrId] = StrId("ext.html")
    namespace: ClassVar[tuple[str, ...]] = ("ext", "html")

    schema: ClassVar[ObjectSchema[HtmlExtConfig]] = ObjectSchema(
        description=(
            "HTML-tools (outline + section). Включаются явно через enable=true."
        ),
        fields=[
            FieldSpec(
                name="enable",
                converter=ChainConverter(Default(False), ParseBool()),
                description="Подключить HTML-tools.",
            ),
            FieldSpec(
                name="tools_allow",
                converter=ParseCsvList(),
                description=(
                    "Whitelist по именам tools (html_outline, html_section). "
                    "Пусто — регистрируются все."
                ),
            ),
        ],
        factory=HtmlExtConfig,
    )
