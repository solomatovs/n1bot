"""Стадия 1: Подготовка загрузки страниц из Confluence.

Создаёт ленивый итератор загрузки в контексте через инжектированную фабрику.
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
        yield StageStarted(stage=self.name)

        if ctx.create_loading_events is None:
            raise ValidationError("create_loading_events не задан в контексте")

        ctx.loading_events = ctx.create_loading_events(ctx)

        yield StageCompleted(stage=self.name, detail="итератор загрузки готов")
