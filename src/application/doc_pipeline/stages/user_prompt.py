"""Стадия: формирование пользовательского промта с контекстом."""
from __future__ import annotations

from typing import Iterator

from application.doc_pipeline.context import DocPipelineContext
from application.doc_pipeline.events import DocPipelineEvent
from domain.doc_chat import LLMMessage, LLMRole
from domain.pipeline import StageCompleted, StageStarted

_USER_TEMPLATE = "Контекст из документов:\n{context}\n\nВопрос: {query}\n\nДай чёткий ответ."


class UserPromptStage:
    """Формирует пользовательский промт (контекст + вопрос) и добавляет в ctx.messages."""

    @property
    def name(self) -> str:
        return "user_prompt"

    def run(self, ctx: DocPipelineContext) -> Iterator[DocPipelineEvent]:
        yield StageStarted(stage=self.name)

        user_message = _USER_TEMPLATE.format(
            context=ctx.expanded_context,
            query=ctx.query,
        )
        ctx.messages.append(LLMMessage(role=LLMRole.USER, content=user_message))

        yield StageCompleted(stage=self.name, detail=f"{len(user_message)} символов")
