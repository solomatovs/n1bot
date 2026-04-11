"""Стадия: добавление системного промта в messages."""
from __future__ import annotations

from typing import Iterator

from application.doc_pipeline.context import DocPipelineContext
from application.doc_pipeline.events import DocPipelineEvent
from domain.doc_chat import LLMMessage, LLMRole
from domain.pipeline import StageCompleted, StageStarted

_SYSTEM_PROMPT = (
    "Ты — эксперт по документации. "
    "У тебя есть инструменты для работы с документами.\n\n"
    "Стратегия работы:\n"
    "1. Используй index_documents чтобы проиндексировать документы перед поиском\n"
    "2. Используй search_documents чтобы найти релевантные фрагменты по теме вопроса\n"
    "3. Используй read_file чтобы прочитать полный контекст вокруг найденных фрагментов\n"
    "4. Используй list_files чтобы узнать какие документы доступны\n"
    "5. Можешь делать несколько поисков с разными запросами для полноты\n"
    "6. Когда у тебя достаточно информации — дай ответ\n\n"
    "Правила:\n"
    "- Всегда начинай с index_documents, если ещё не индексировал\n"
    "- Отвечай ТОЛЬКО на основе найденной информации из документов\n"
    "- Если документы не содержат ответа, скажи об этом прямо\n"
    "- Указывай источники (файл и строки) в ответе"
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
