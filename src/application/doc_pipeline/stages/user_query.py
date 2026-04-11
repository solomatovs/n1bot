"""Стадия: добавление вопроса пользователя (без context-stuffing).

В агентном режиме контекст формируется LLM через tool calls,
поэтому вопрос передаётся как есть.
"""
from __future__ import annotations

from typing import Iterator

from application.doc_pipeline.context import DocPipelineContext
from application.doc_pipeline.events import DocPipelineEvent
from domain.doc_chat import LLMMessage, LLMRole
from domain.pipeline import StageCompleted, StageStarted


class UserQueryStage:
    """Добавляет сырой вопрос пользователя в ctx.messages."""

    @property
    def name(self) -> str:
        return "user_query"

    def run(self, ctx: DocPipelineContext) -> Iterator[DocPipelineEvent]:
        yield StageStarted(stage=self.name)
        ctx.messages.append(LLMMessage(role=LLMRole.USER, content=ctx.query))
        yield StageCompleted(stage=self.name, detail=f"{len(ctx.query)} символов")
