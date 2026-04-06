"""Стадия 10: Стриминг ответа от LLM с парсингом <think> тегов."""
from __future__ import annotations

from typing import Iterator

from events import AnswerToken, ChatEvent, GenerationDone, ThinkingToken
from pipeline.context import PipelineContext
from pipeline.events import StageCompleted, StageStarted


class LLMStreamStage:

    @property
    def name(self) -> str:
        return "llm_stream"

    def run(self, ctx: PipelineContext) -> Iterator[ChatEvent]:
        yield StageStarted(stage=self.name)

        assert ctx.messages is not None

        stream = ctx.openai_client.chat.completions.create(
            model=ctx.model,
            messages=ctx.messages,
            stream=True,
            **ctx.search_params.llm_kwargs(),
        )

        in_think = False

        for chunk in stream:
            delta = chunk.choices[0].delta

            reasoning = getattr(delta, "reasoning_content", None) or ""
            if reasoning:
                yield ThinkingToken(token=reasoning)

            content = getattr(delta, "content", None) or ""
            if not content:
                continue

            for role, text, new_in_think in _parse_think_tags(content, in_think):
                in_think = new_in_think
                if not text:
                    continue
                if role == "thinking":
                    yield ThinkingToken(token=text)
                else:
                    yield AnswerToken(token=text)

        yield GenerationDone()
        yield StageCompleted(stage=self.name, detail="генерация завершена")


def _parse_think_tags(token: str, in_think: bool) -> Iterator[tuple[str, str, bool]]:
    """Разбирает токен на фрагменты (role, text, new_in_think)."""
    buf = token
    while buf:
        if in_think:
            end = buf.find("</think>")
            if end != -1:
                yield ("thinking", buf[:end], False)
                buf = buf[end + len("</think>"):]
                in_think = False
            else:
                yield ("thinking", buf, True)
                buf = ""
        else:
            start = buf.find("<think>")
            if start != -1:
                if start > 0:
                    yield ("answer", buf[:start], True)
                buf = buf[start + len("<think>"):]
                in_think = True
            else:
                yield ("answer", buf, False)
                buf = ""
