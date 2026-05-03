"""Конфиг-секция [indexer.sources.fs] для FsSource."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
    ParseBool,
    ParseCsvList,
)
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema

__all__ = ["FsSourceConfig", "FsSourceConfigSection"]


@dataclass(frozen=True)
class FsSourceConfig:
    """DTO параметров FsSource."""

    paths: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    follow_symlinks: bool = False


class FsSourceConfigSection(ConfigSection[FsSourceConfig]):
    """Параметры FsSource: что и как обходим в файловой системе."""

    namespace: ClassVar[tuple[str, ...]] = ("indexer", "sources", "fs")

    schema: ClassVar[ObjectSchema[FsSourceConfig]] = ObjectSchema(
        description=(
            "Параметры filesystem-source для индексатора: список корневых путей "
            "и фильтры include/exclude по glob'ам."
        ),
        fields=[
            FieldSpec(
                name="paths",
                coercer=ChainCoercer(Default(""), ParseCsvList()),
                description=(
                    "Корневые файлы или директории для обхода. CSV в env, "
                    "TOML-array в файле. Пусто — нечего индексировать."
                ),
            ),
            FieldSpec(
                name="include",
                coercer=ChainCoercer(Default(""), ParseCsvList()),
                description=(
                    "Glob'ы для include-фильтра (например *.md, *.html). "
                    "Пусто — пропускать всё."
                ),
            ),
            FieldSpec(
                name="exclude",
                coercer=ChainCoercer(Default(""), ParseCsvList()),
                description="Glob'ы для exclude-фильтра поверх include.",
            ),
            FieldSpec(
                name="follow_symlinks",
                coercer=ChainCoercer(Default(False), ParseBool()),
                description="Идти ли по symlink'ам при обходе директории.",
            ),
        ],
        factory=FsSourceConfig,
    )
