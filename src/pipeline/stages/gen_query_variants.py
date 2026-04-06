"""Стадия 2: Генерация переформулировок запроса (multi-query)."""
from __future__ import annotations

import logging
from typing import Iterator

from openai import APIError as OpenAIAPIError

from events import ChatEvent
from pipeline.context import PipelineContext
from pipeline.events import QueryVariantsGenerated, StageCompleted, StageStarted

log = logging.getLogger(__name__)


class GenQueryVariantsStage:

    @property
    def name(self) -> str:
        return "gen_query_variants"

    def run(self, ctx: PipelineContext) -> Iterator[ChatEvent]:
        yield StageStarted(stage=self.name)

        if not ctx.search_params.use_multi_query:
            ctx.query_variants = [ctx.query]
            yield StageCompleted(stage=self.name, detail="multi-query выключен")
            return

        n = ctx.search_params.mq_variants
        prompt = f"Дай {n} кратких переформулировок запроса; по одной на строку.\nЗапрос: {ctx.query}"

        try:
            r = ctx.openai_client.chat.completions.create(
                model=ctx.model,
                temperature=ctx.retrieval_config.mq_temperature,
                messages=[{"role": "user", "content": prompt}],
            )
            content = r.choices[0].message.content
            if not content:
                log.warning("Модель вернула пустой ответ при генерации переформулировок")
                ctx.query_variants = [ctx.query]
            else:
                lines = [s.strip("- ").strip() for s in content.splitlines() if s.strip()]
                ctx.query_variants = [ctx.query] + lines[:n]
        except OpenAIAPIError as e:
            log.warning("Не удалось сгенерировать переформулировки: %s", e)
            ctx.query_variants = [ctx.query]

        yield QueryVariantsGenerated(variants=ctx.query_variants)
        yield StageCompleted(stage=self.name, detail=f"{len(ctx.query_variants)} вариантов")
