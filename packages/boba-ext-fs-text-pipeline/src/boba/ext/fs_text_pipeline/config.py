"""ConfigSection [indexer.pipelines.fs_text]."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
    MinValue,
    ParseBool,
    ParseCsvList,
    ParseInt,
)
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema

__all__ = ["FsTextPipelineConfig", "FsTextPipelineConfigSection"]


@dataclass(frozen=True)
class FsTextPipelineConfig:
    paths: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    follow_symlinks: bool = False
    chunk_size: int = 1000
    chunk_overlap: int = 200


class FsTextPipelineConfigSection(ConfigSection[FsTextPipelineConfig]):
    """Pipeline FS-обхода + TextReader (plain UTF-8) + sliding chunker."""

    namespace: ClassVar[tuple[str, ...]] = ("indexer", "pipelines", "fs_text")

    schema: ClassVar[ObjectSchema[FsTextPipelineConfig]] = ObjectSchema(
        description=(
            "Pipeline 'ext.fs_text': обход директорий + чтение .txt/.log как "
            "UTF-8 → одна Section на файл (без heading-структуры) → sliding "
            "chunker → ChromaDB."
        ),
        fields=[
            FieldSpec(
                name="paths",
                coercer=ParseCsvList(),
                description="Файлы и/или директории для обхода.",
            ),
            FieldSpec(
                name="include",
                coercer=ParseCsvList(),
                description="Glob-паттерны включения, например '*.txt,*.log'.",
            ),
            FieldSpec(
                name="exclude",
                coercer=ParseCsvList(),
                description="Glob-паттерны исключения.",
            ),
            FieldSpec(
                name="follow_symlinks",
                coercer=ChainCoercer(Default(False), ParseBool()),
                description="Следовать за symlink-ами.",
            ),
            FieldSpec(
                name="chunk_size",
                coercer=ChainCoercer(Default(1000), ParseInt(), MinValue(1)),
                description="Размер чанка sliding-window (символов).",
            ),
            FieldSpec(
                name="chunk_overlap",
                coercer=ChainCoercer(Default(200), ParseInt()),
                description="Перекрытие соседних чанков.",
            ),
        ],
        factory=FsTextPipelineConfig,
    )
