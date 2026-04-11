"""Стадия: добавление системного промта в messages."""
from __future__ import annotations

from typing import Iterator

from application.doc_pipeline.context import DocPipelineContext
from application.doc_pipeline.events import DocPipelineEvent
from domain.doc_chat import LLMMessage, LLMRole
from domain.pipeline import StageCompleted, StageStarted

_SYSTEM_PROMPT = (
    "Ты — эксперт по документации. "
    "Отвечай ТОЛЬКО на основе предоставленного контекста из документов. "
    "Если контекст не содержит ответа, скажи об этом прямо. "
    "Указывай источники (файл и строки), откуда взята информация."
)


class SystemPromptStage:
    """Добавляет системный промт в ctx.messages."""

    @property
    def name(self) -> str:
        return "system_prompt"

    def run(self, ctx: DocPipelineContext) -> Iterator[DocPipelineEvent]:
        yield StageStarted(stage=self.name)
        ctx.messages.append(LLMMessage(role=LLMRole.SYSTEM, content=_SYSTEM_PROMPT))
        yield StageCompleted(stage=self.name, detail="добавлен")
