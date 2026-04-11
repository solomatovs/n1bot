"""Стадия: генерация ответа LLM.

Берёт ctx.messages (уже собранные предыдущими стадиями) и стримит ответ.
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


class GenerateStage:
    """Отправляет ctx.messages в LLM, стримит токены и размышления."""

    _NO_CONTEXT_MESSAGE = "Не удалось найти релевантный контекст в документах."

    def __init__(self, openai_client: OpenAI) -> None:
        self._client = openai_client

    @property
    def name(self) -> str:
        return "generate"

    def run(self, ctx: DocPipelineContext) -> Iterator[DocPipelineEvent]:
        yield StageStarted(stage=self.name)

        if not ctx.expanded_context:
            ctx.answer = self._NO_CONTEXT_MESSAGE
            yield AnswerToken(token=ctx.answer)
            yield GenerationDone()
            yield StageCompleted(stage=self.name, detail="нет контекста")
            return

        stream = self._client.chat.completions.create(
            model=ctx.model,
            messages=[m.to_dict() for m in ctx.messages],  # type: ignore[arg-type]
            stream=True,
        )

        answer_tokens: list[str] = []
        parser = _ThinkTagParser()

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

            for fragment in parser.feed(content):
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


class _ThinkTagParser:
    """Stateful парсер <think>...</think> тегов в потоке токенов."""

    _TAG_OPEN = "<think>"
    _TAG_CLOSE = "</think>"

    def __init__(self) -> None:
        self._in_think = False

    def feed(self, token: str) -> Iterator[_ThinkFragment]:
        """Разобрать токен на фрагменты thinking/answer."""
        buf = token
        while buf:
            if self._in_think:
                end = buf.find(self._TAG_CLOSE)
                if end != -1:
                    yield _ThinkFragment(_FragmentRole.THINKING, buf[:end])
                    buf = buf[end + len(self._TAG_CLOSE):]
                    self._in_think = False
                else:
                    yield _ThinkFragment(_FragmentRole.THINKING, buf)
                    buf = ""
            else:
                start = buf.find(self._TAG_OPEN)
                if start != -1:
                    if start > 0:
                        yield _ThinkFragment(_FragmentRole.ANSWER, buf[:start])
                    buf = buf[start + len(self._TAG_OPEN):]
                    self._in_think = True
                else:
                    yield _ThinkFragment(_FragmentRole.ANSWER, buf)
                    buf = ""
