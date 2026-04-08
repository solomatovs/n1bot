"""Стадия 9: Сборка системного промпта и сообщения для LLM."""
from __future__ import annotations

from typing import Iterator

from application.query_pipeline.events import ChatEvent
from domain.pipeline.events import StageCompleted, StageStarted
from application.query_pipeline.context import QueryContext


class BuildMessagesStage:

    @property
    def name(self) -> str:
        return "build_messages"

    def run(self, ctx: QueryContext) -> Iterator[ChatEvent]:
        yield StageStarted(stage=self.name)

        assert ctx.context_text is not None

        user_content = ctx.prompt_params.format_user_message(
            context=ctx.context_text, query=ctx.query,
        )

        ctx.messages = [
            {"role": "system", "content": ctx.prompt_params.system_prompt},
            {"role": "user", "content": user_content},
        ]

        yield StageCompleted(stage=self.name, detail="сообщения собраны")
