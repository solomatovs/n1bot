"""Стадия 1: Подготовка загрузки страниц из Confluence.

Создаёт ленивый итератор загрузки в контексте.
Сами страницы загружаются лениво через ChunkStage → StoreStage —
это сохраняет per-page streaming (загрузка → чанкинг → сохранение).
"""
from __future__ import annotations

import logging
from typing import Iterator, Union

from domain.errors import ValidationError
from domain.pipeline import StageCompleted, StageStarted
from application.load_pipeline.context import LoadContext

log = logging.getLogger(__name__)

LoadPagesEvent = Union[StageStarted, StageCompleted]


class LoadPagesStage:
    """Настраивает ленивый итератор загрузки в контексте."""

    @property
    def name(self) -> str:
        return "load_pages"

    def run(self, ctx: LoadContext) -> Iterator[LoadPagesEvent]:
        from adapters.confluence import BatchPageLoader, PageLoader, SpaceLoader

        yield StageStarted(stage=self.name)

        page_loader = PageLoader(ctx.cfg, ctx.confluence_request_params)
        batch_loader = BatchPageLoader(page_loader)

        if ctx.space_key:
            if ctx.space_params is None:
                raise ValidationError("space_params обязателен при загрузке пространства")
            space_loader = SpaceLoader(batch_loader, ctx.cfg, ctx.space_params, ctx.confluence_request_params)
            ctx.loading_events = space_loader.load(ctx.space_key)
        elif ctx.page_ids:
            ctx.loading_events = batch_loader.load(ctx.page_ids)
        else:
            raise ValidationError("Необходимо задать space_key или page_ids")

        yield StageCompleted(stage=self.name, detail="итератор загрузки готов")
