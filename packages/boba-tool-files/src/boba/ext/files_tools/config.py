"""Конфигурация files-extension."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, ParseBool, ParseCsvList
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema

__all__ = ["FilesExtConfig", "FilesSection"]


@dataclass(frozen=True)
class FilesExtConfig:
    enable: bool = False
    tools_allow: list[str] = field(default_factory=list)


class FilesSection(ConfigSection[FilesExtConfig]):
    """Секция конфига files-extension."""

    namespace: ClassVar[tuple[str, ...]] = ("ext", "files")

    schema: ClassVar[ObjectSchema[FilesExtConfig]] = ObjectSchema(
        description=(
            "Файловые tools (cat/ls/grep/edit/...). "
            "Включаются явно через enable=true; tools_allow позволяет выбрать "
            "подмножество имён tools (пусто — все)."
        ),
        fields=[
            FieldSpec(
                name="enable",
                coercer=ChainCoercer(Default(False), ParseBool()),
                description="Подключить файловые tools.",
            ),
            FieldSpec(
                name="tools_allow",
                coercer=ParseCsvList(),
                description=(
                    "Whitelist по именам tools (cat, ls, grep, ...). "
                    "Пусто — регистрируются все."
                ),
            ),
        ],
        factory=FilesExtConfig,
    )
