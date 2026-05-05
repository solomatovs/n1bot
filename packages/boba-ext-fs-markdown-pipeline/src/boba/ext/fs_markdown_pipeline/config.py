"""ConfigSection [indexer.pipelines.fs_markdown]."""

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

__all__ = ["FsMarkdownPipelineConfig", "FsMarkdownPipelineConfigSection"]


@dataclass(frozen=True)
class FsMarkdownPipelineConfig:
    paths: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    follow_symlinks: bool = False
    chunk_size: int = 1500
    chunk_overlap: int = 150


class FsMarkdownPipelineConfigSection(ConfigSection[FsMarkdownPipelineConfig]):
    """Pipeline FS-обхода + MarkdownReader + heading chunker."""

    namespace: ClassVar[tuple[str, ...]] = ("indexer", "pipelines", "fs_markdown")

    schema: ClassVar[ObjectSchema[FsMarkdownPipelineConfig]] = ObjectSchema(
        description=(
            "Параметры pipeline'а 'ext.fs_markdown': обход директорий + парсинг "
            ".md → heading-aware Section'ы → heading chunker → ChromaDB store."
        ),
        fields=[
            FieldSpec(
                name="paths",
                coercer=ParseCsvList(),
                description=(
                    "Файлы и/или директории для обхода. Директории обходятся "
                    "рекурсивно (rglob). Скрытые `.dot`-пути пропускаются. "
                    "Обязателен для запуска pipeline'а; default — пусто, "
                    "чтобы установка пакета не валила приложение, не "
                    "использующее этот pipeline."
                ),
            ),
            FieldSpec(
                name="include",
                coercer=ParseCsvList(),
                description=(
                    "Glob-паттерны включения (по имени и относительному пути). "
                    "Пусто — без фильтра. Пример: '*.md,*.markdown'."
                ),
            ),
            FieldSpec(
                name="exclude",
                coercer=ParseCsvList(),
                description="Glob-паттерны исключения.",
            ),
            FieldSpec(
                name="follow_symlinks",
                coercer=ChainCoercer(Default(False), ParseBool()),
                description="Следовать за symlink-ами (риск циклов).",
            ),
            FieldSpec(
                name="chunk_size",
                coercer=ChainCoercer(Default(1500), ParseInt(), MinValue(1)),
                description="Целевой размер чанка (символов).",
            ),
            FieldSpec(
                name="chunk_overlap",
                coercer=ChainCoercer(Default(150), ParseInt()),
                description="Перекрытие sub-чанков внутри одной Section.",
            ),
        ],
        factory=FsMarkdownPipelineConfig,
    )
