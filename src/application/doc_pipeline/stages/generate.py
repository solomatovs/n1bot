"""Стадия 4: Генерация ответа LLM на основе найденного контекста."""
from __future__ import annotations

import logging
from typing import Iterator

from openai import OpenAI

from application.doc_pipeline.context import DocPipelineContext
from application.doc_pipeline.events import AnswerToken, DocPipelineEvent, GenerationDone
from domain.pipeline import StageCompleted, StageStarted

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Ты — эксперт по документации. "
    "Отвечай ТОЛЬКО на основе предоставленного контекста из документов. "
    "Если контекст не содержит ответа, скажи об этом прямо. "
    "Указывай источники (файл и строки), откуда взята информация."
)

_USER_TEMPLATE = "Контекст из документов:\n{context}\n\nВопрос: {query}\n\nДай чёткий ответ."


class GenerateStage:
    """Генерирует ответ через LLM, стримя токены."""

    def __init__(self, openai_client: OpenAI) -> None:
        self._client = openai_client

    @property
    def name(self) -> str:
        return "generate"

    def run(self, ctx: DocPipelineContext) -> Iterator[DocPipelineEvent]:
        yield StageStarted(stage=self.name)

        if not ctx.expanded_context:
            ctx.answer = "Не удалось найти релевантный контекст в документах."
            yield AnswerToken(token=ctx.answer)
            yield GenerationDone()
            yield StageCompleted(stage=self.name, detail="нет контекста")
            return

        user_message = _USER_TEMPLATE.format(
            context=ctx.expanded_context,
            query=ctx.query,
        )

        stream = self._client.chat.completions.create(
            model=ctx.model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            stream=True,
        )

        tokens: list[str] = []
        for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                tokens.append(delta.content)
                yield AnswerToken(token=delta.content)

        ctx.answer = "".join(tokens)
        yield GenerationDone()
        yield StageCompleted(stage=self.name, detail=f"{len(ctx.answer)} символов")
