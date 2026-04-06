"""Стадия 1: Подготовка загрузки страниц из Confluence.

Создаёт ленивый итератор загрузки в контексте.
Сами страницы загружаются лениво на стадии ChunkAndStoreStage —
это сохраняет per-page streaming (загрузка → чанкинг → сохранение).
"""
from __future__ import annotations

import logging
from typing import Iterator, Union

from pipeline.events import StageCompleted, StageStarted
from load_pipeline.context import LoadContext
from loaders import BatchPageLoader, PageLoader, SpaceLoader

log = logging.getLogger(__name__)

LoadPagesEvent = Union[StageStarted, StageCompleted]


class LoadPagesStage:
    """Настраивает ленивый итератор загрузки в контексте."""

    @property
    def name(self) -> str:
        return "load_pages"

    def run(self, ctx: LoadContext) -> Iterator[LoadPagesEvent]:
        yield StageStarted(stage=self.name)

        page_loader = PageLoader(ctx.cfg)
        batch_loader = BatchPageLoader(page_loader)

        if ctx.space_key:
            space_loader = SpaceLoader(batch_loader, ctx.cfg, ctx.space_params)
            ctx.loading_events = space_loader.load(ctx.space_key)
        else:
            ctx.loading_events = batch_loader.load(ctx.page_ids)

        yield StageCompleted(stage=self.name, detail="итератор загрузки готов")
