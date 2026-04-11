"""Стадия: загрузка истории чата в messages."""
from __future__ import annotations

from typing import Iterator

from application.doc_pipeline.context import DocPipelineContext
from application.doc_pipeline.events import DocPipelineEvent
from domain.doc_chat import JsonlChatReader, LLMMessage, LLMRole
from domain.pipeline import StageCompleted, StageStarted


class HistoryStage:
    """Читает историю из файла, добавляет user/assistant в ctx.messages."""

    @property
    def name(self) -> str:
        return "history"

    def run(self, ctx: DocPipelineContext) -> Iterator[DocPipelineEvent]:
        yield StageStarted(stage=self.name)

        count = 0
        if ctx.history_path is not None:
            with JsonlChatReader(ctx.history_path) as reader:
                for event in reader.read():
                    if event.is_user:
                        ctx.messages.append(LLMMessage(role=LLMRole.USER, content=event.content))
                        count += 1
                    elif event.is_assistant:
                        ctx.messages.append(LLMMessage(role=LLMRole.ASSISTANT, content=event.content))
                        count += 1

        yield StageCompleted(stage=self.name, detail=f"{count} сообщений")
