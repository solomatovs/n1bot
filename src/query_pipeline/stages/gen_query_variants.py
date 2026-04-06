"""Стадия 2: Генерация переформулировок запроса (multi-query)."""
from __future__ import annotations

import logging
import time
from typing import Iterator

from openai import APIError as OpenAIAPIError, APITimeoutError

from pipeline.events import StageCompleted, StageStarted
from query_pipeline.context import QueryContext
from query_pipeline.events import ChatEvent, QueryVariantsGenerated

log = logging.getLogger(__name__)


class GenQueryVariantsStage:

    @property
    def name(self) -> str:
        return "gen_query_variants"

    def run(self, ctx: QueryContext) -> Iterator[ChatEvent]:
        yield StageStarted(stage=self.name)

        if not ctx.search_params.use_multi_query:
            ctx.query_variants = [ctx.query]
            yield StageCompleted(stage=self.name, detail="multi-query выключен")
            return

        n = ctx.search_params.mq_variants
        prompt = ctx.search_params.mq_prompt_template.format(n=n, query=ctx.query)

        start = time.monotonic()
        try:
            r = ctx.openai_client.chat.completions.create(
                model=ctx.model,
                temperature=ctx.retrieval_config.mq_temperature,
                max_tokens=ctx.retrieval_config.mq_max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            content = r.choices[0].message.content
            if not content:
                log.warning("Model returned empty response for query variants generation")
                ctx.query_variants = [ctx.query]
            else:
                lines = [s.strip("- ").strip() for s in content.splitlines() if s.strip()]
                ctx.query_variants = [ctx.query] + lines[:n]
        except APITimeoutError as e:
            elapsed = time.monotonic() - start
            log.warning(
                "LLM request timed out after %.1fs. model=%s, prompt_len=%d: %s",
                elapsed, ctx.model, len(prompt), e,
            )
            ctx.query_variants = [ctx.query]
        except OpenAIAPIError as e:
            elapsed = time.monotonic() - start
            log.warning(
                "LLM request failed after %.1fs. model=%s, prompt_len=%d: %s",
                elapsed, ctx.model, len(prompt), e,
            )
            ctx.query_variants = [ctx.query]

        yield QueryVariantsGenerated(variants=ctx.query_variants)
        yield StageCompleted(stage=self.name, detail=f"{len(ctx.query_variants)} вариантов")
