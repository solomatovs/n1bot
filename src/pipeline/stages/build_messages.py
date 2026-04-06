"""Стадия 9: Сборка системного промпта и сообщения для LLM."""
from __future__ import annotations

from typing import Iterator

from events import ChatEvent
from pipeline.context import PipelineContext
from pipeline.events import StageCompleted, StageStarted


class BuildMessagesStage:

    @property
    def name(self) -> str:
        return "build_messages"

    def run(self, ctx: PipelineContext) -> Iterator[ChatEvent]:
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
