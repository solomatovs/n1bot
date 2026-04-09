"""Стадия 10: Стриминг ответа от LLM с парсингом <think> тегов."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterator

from openai import APIError as OpenAIAPIError, APITimeoutError

from application.query_pipeline.events import AnswerToken, ChatEvent, GenerationDone, ThinkingToken
from domain.pipeline import StageCompleted, StageStarted
from application.query_pipeline.context import QueryContext

log = logging.getLogger(__name__)


class FragmentRole(Enum):
    """Роль фрагмента в потоке LLM-ответа."""
    THINKING = "thinking"
    ANSWER = "answer"


@dataclass(frozen=True)
class ThinkTagFragment:
    """Фрагмент, извлечённый парсером <think> тегов."""
    role: FragmentRole
    text: str
    in_think: bool


class LLMStreamStage:

    @property
    def name(self) -> str:
        return "llm_stream"

    @staticmethod
    def _extract_reasoning(delta: object) -> str:
        """Извлечь reasoning_content — нестандартное поле, добавляемое
        некоторыми моделями (DeepSeek, QwQ) через LiteLLM; отсутствует в OpenAI SDK."""
        return getattr(delta, "reasoning_content", None) or ""

    @staticmethod
    def _extract_content(delta: object) -> str:
        """Извлечь текстовый контент из дельты стриминга."""
        return getattr(delta, "content", None) or ""

    def run(self, ctx: QueryContext) -> Iterator[ChatEvent]:
        yield StageStarted(stage=self.name)

        assert ctx.messages is not None

        llm_kwargs = ctx.search_params.llm_kwargs()
        start = time.monotonic()

        try:
            stream = ctx.openai_client.chat.completions.create(
                model=ctx.model,
                messages=ctx.messages,
                stream=True,
                **llm_kwargs,
            )
        except APITimeoutError as e:
            elapsed = time.monotonic() - start
            raise ConnectionError(
                f"LLM stream request timed out after {elapsed:.1f}s. "
                f"model={ctx.model}, params={llm_kwargs}"
            ) from e
        except OpenAIAPIError as e:
            elapsed = time.monotonic() - start
            raise ConnectionError(
                f"LLM stream request failed after {elapsed:.1f}s. "
                f"model={ctx.model}, params={llm_kwargs}: {e}"
            ) from e

        in_think = False

        try:
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                reasoning = self._extract_reasoning(delta)
                if reasoning:
                    yield ThinkingToken(token=reasoning)

                content = self._extract_content(delta)
                if not content:
                    continue

                for fragment in _parse_think_tags(content, in_think):
                    in_think = fragment.in_think
                    if not fragment.text:
                        continue
                    match fragment.role:
                        case FragmentRole.THINKING:
                            yield ThinkingToken(token=fragment.text)
                        case FragmentRole.ANSWER:
                            yield AnswerToken(token=fragment.text)
        except APITimeoutError as e:
            elapsed = time.monotonic() - start
            raise ConnectionError(
                f"LLM stream timed out after {elapsed:.1f}s during token streaming. "
                f"model={ctx.model}"
            ) from e

        yield GenerationDone()
        yield StageCompleted(stage=self.name, detail="генерация завершена")


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def _parse_think_tags(token: str, in_think: bool) -> Iterator[ThinkTagFragment]:
    """Разбирает токен на фрагменты с учётом <think>...</think> тегов."""
    buf = token
    while buf:
        if in_think:
            end = buf.find(_THINK_CLOSE)
            if end != -1:
                yield ThinkTagFragment(role=FragmentRole.THINKING, text=buf[:end], in_think=False)
                buf = buf[end + len(_THINK_CLOSE):]
                in_think = False
            else:
                yield ThinkTagFragment(role=FragmentRole.THINKING, text=buf, in_think=True)
                buf = ""
        else:
            start = buf.find(_THINK_OPEN)
            if start != -1:
                if start > 0:
                    yield ThinkTagFragment(role=FragmentRole.ANSWER, text=buf[:start], in_think=True)
                buf = buf[start + len(_THINK_OPEN):]
                in_think = True
            else:
                yield ThinkTagFragment(role=FragmentRole.ANSWER, text=buf, in_think=False)
                buf = ""
                buf = ""
