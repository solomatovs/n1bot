"""Стадия 4: Генерация ответа LLM на основе найденного контекста.

Поддерживает thinking-токены (reasoning_content и <think> теги).
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Iterator, NamedTuple

from openai import OpenAI

from application.doc_pipeline.context import DocPipelineContext
from application.doc_pipeline.events import (
    AnswerToken,
    DocPipelineEvent,
    GenerationDone,
    ThinkingToken,
)
from domain.pipeline import StageCompleted, StageStarted

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Ты — эксперт по документации. "
    "Отвечай ТОЛЬКО на основе предоставленного контекста из документов. "
    "Если контекст не содержит ответа, скажи об этом прямо. "
    "Указывай источники (файл и строки), откуда взята информация."
)

_USER_TEMPLATE = "Контекст из документов:\n{context}\n\nВопрос: {query}\n\nДай чёткий ответ."

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


class GenerateStage:
    """Генерирует ответ через LLM, стримя токены и размышления."""

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

        answer_tokens: list[str] = []
        in_think = False

        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # reasoning_content — нестандартное поле (DeepSeek, QwQ)
            reasoning = getattr(delta, "reasoning_content", None) or ""
            if reasoning:
                yield ThinkingToken(token=reasoning)

            content = getattr(delta, "content", None) or ""
            if not content:
                continue

            for fragment in _parse_think_tags(content, in_think):
                in_think = fragment.in_think
                if not fragment.text:
                    continue
                if fragment.role is _FragmentRole.THINKING:
                    yield ThinkingToken(token=fragment.text)
                else:
                    answer_tokens.append(fragment.text)
                    yield AnswerToken(token=fragment.text)

        ctx.answer = "".join(answer_tokens)
        yield GenerationDone()
        yield StageCompleted(stage=self.name, detail=f"{len(ctx.answer)} символов")


# ---------------------------------------------------------------------------
# Парсинг <think> тегов
# ---------------------------------------------------------------------------

class _FragmentRole(Enum):
    THINKING = "thinking"
    ANSWER = "answer"


class _ThinkFragment(NamedTuple):
    role: _FragmentRole
    text: str
    in_think: bool


def _parse_think_tags(token: str, in_think: bool) -> Iterator[_ThinkFragment]:
    """Разбирает токен на фрагменты с учётом <think>...</think> тегов."""
    buf = token
    while buf:
        if in_think:
            end = buf.find(_THINK_CLOSE)
            if end != -1:
                yield _ThinkFragment(_FragmentRole.THINKING, buf[:end], False)
                buf = buf[end + len(_THINK_CLOSE):]
                in_think = False
            else:
                yield _ThinkFragment(_FragmentRole.THINKING, buf, True)
                buf = ""
        else:
            start = buf.find(_THINK_OPEN)
            if start != -1:
                if start > 0:
                    yield _ThinkFragment(_FragmentRole.ANSWER, buf[:start], True)
                buf = buf[start + len(_THINK_OPEN):]
                in_think = True
            else:
                yield _ThinkFragment(_FragmentRole.ANSWER, buf, False)
                buf = ""
