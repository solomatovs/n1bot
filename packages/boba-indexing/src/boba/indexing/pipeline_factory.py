"""PipelineFactory + PipelineRegistry: сборка готовых IndexPipeline'ов.

Pipeline-плагин экспортирует `PipelineFactory` через entry-point
`boba.indexing.pipelines`. Внутри фабрика **явно** импортирует все нужные
компоненты (RequestSource / Transport / Reader / Chunker / Store) и
собирает `IndexPipeline`. Сами компоненты — обычные библиотеки,
не плагинизированы.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from boba.indexing.context import PipelineId
from boba.indexing.extension import IndexerExtensionContext
from boba.indexing.pipeline import IndexPipeline
from boba.patterns import ContextItemProvider

__all__ = ["PipelineFactory", "PipelineRegistry"]


class PipelineFactory(
    ContextItemProvider[IndexerExtensionContext, PipelineId, IndexPipeline[Any]],
    ABC,
):
    """Фабрика готового IndexPipeline для CLI-плагина.

    `id()` — `PipelineId` (например `ext.fs_markdown`, `ext.confluence_space`).
    `produce(ctx)` — читает свою ConfigSection через `ctx.config.section(...)`
    и собирает конкретный IndexPipeline.
    """

    @abstractmethod
    def id(self) -> PipelineId: ...

    @abstractmethod
    def produce(self, ctx: IndexerExtensionContext) -> IndexPipeline[Any]: ...


class PipelineRegistry:
    """Каталог зарегистрированных PipelineFactory.

    Lazy: pipeline'ы НЕ собираются при регистрации (`produce()` валидирует
    конфиг, и валидация одного pipeline-плагина не должна валить остальные).
    Caller выбирает нужный id и сам вызывает `factory.produce(ctx)`.
    """

    def __init__(self) -> None:
        self._factories: dict[PipelineId, PipelineFactory] = {}

    def register_factory(self, factory: PipelineFactory) -> None:
        self._factories[factory.id()] = factory

    def factories(self) -> dict[PipelineId, PipelineFactory]:
        """Все зарегистрированные фабрики; caller дёргает produce() при нужде."""
        return dict(self._factories)

    def get(self, pipeline_id: PipelineId) -> PipelineFactory | None:
        return self._factories.get(pipeline_id)

    def __contains__(self, pipeline_id: object) -> bool:
        return pipeline_id in self._factories
