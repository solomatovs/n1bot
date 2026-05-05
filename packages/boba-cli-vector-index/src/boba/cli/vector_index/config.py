"""Конфиг runner'а boba-cli-vector-index — выбор pipeline по id + общие поля."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
    Nullable,
    OneOf,
    ParseBool,
    ParseInt,
    ParseString,
)
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema

__all__ = ["ACTIONS", "VectorIndexConfig", "VectorIndexSection"]


ACTIONS: frozenset[str] = frozenset(
    {"index", "list", "delete", "sync", "show"}
)


@dataclass(frozen=True)
class VectorIndexConfig:
    """Параметры одного запуска CLI-runner'а."""

    action: str
    collection: str
    description: str | None
    confirm_skip: bool
    verbose: int
    pipeline: str
    show_source_id: str | None
    show_limit: int
    show_snippet_chars: int


class VectorIndexSection(ConfigSection[VectorIndexConfig]):
    """Секция [vector_index] — runner CLI."""

    namespace: ClassVar[tuple[str, ...]] = ("vector_index",)

    schema: ClassVar[ObjectSchema[VectorIndexConfig]] = ObjectSchema(
        description=(
            "Параметры одного запуска CLI: action + collection + pipeline-id. "
            "Параметры самого pipeline'а — в его собственной секции "
            "[indexer.pipelines.<pipeline_id>]."
        ),
        fields=[
            FieldSpec(
                name="action",
                coercer=ChainCoercer(ParseString(), OneOf(*sorted(ACTIONS))),
                required=True,
                description=(
                    f"Что делать: один из {sorted(ACTIONS)}. Обязательно."
                ),
            ),
            FieldSpec(
                name="collection",
                coercer=ChainCoercer(Default(""), ParseString()),
                description=(
                    "Имя коллекции в Store. Обязательно для "
                    "index/sync/delete/show."
                ),
            ),
            FieldSpec(
                name="description",
                coercer=Nullable(ParseString()),
                description=(
                    "Описание коллекции (создаётся при первом index)."
                ),
            ),
            FieldSpec(
                name="confirm_skip",
                coercer=ChainCoercer(Default(False), ParseBool()),
                description=(
                    "Пропустить интерактивное подтверждение (action=delete)."
                ),
            ),
            FieldSpec(
                name="verbose",
                coercer=ChainCoercer(Default(0), ParseInt()),
                description="0=WARN, 1=INFO, 2=DEBUG.",
            ),
            FieldSpec(
                name="pipeline",
                coercer=ChainCoercer(Default(""), ParseString()),
                description=(
                    "PipelineId плагина (entry-point boba.indexing.pipelines). "
                    "Обязательно для index/sync. Например: ext.fs_markdown, "
                    "ext.confluence_space."
                ),
            ),
            FieldSpec(
                name="show_source_id",
                coercer=Nullable(ParseString()),
                description=(
                    "action=show: фильтр по конкретному source_id. Пусто — "
                    "все чанки коллекции."
                ),
            ),
            FieldSpec(
                name="show_limit",
                coercer=ChainCoercer(Default(20), ParseInt()),
                description="action=show: сколько чанков выводить (default 20).",
            ),
            FieldSpec(
                name="show_snippet_chars",
                coercer=ChainCoercer(Default(200), ParseInt()),
                description=(
                    "action=show: длина text-preview каждого чанка (default 200)."
                ),
            ),
        ],
        factory=VectorIndexConfig,
    )
