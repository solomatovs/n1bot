"""JsonContentToolCallMiddleware: JSON в content → синтез tool-call deltas.

Покрывает кейс «модель поддерживает tools, но провайдер не маршрутизирует их
как tool_calls» (например, ollama, проброшенная через litellm на старый
`/v1/generate`-эндпоинт). Модель в ответ вместо `tool_calls`-структуры
эмитит JSON-объект `{"name": "...", "arguments": {...}}` в обычном `content`.

Middleware ставится через `LLMBuilder.use_provider_middleware(...)` —
то есть **под** `AssistantAggregator`, на сыром delta-потоке провайдера.
Если первый непустой символ ответа — `{`, то Answer-токены не пробрасываются
наружу, а буферизуются; на `LLMGenerationDone` буфер парсится:

  - распарсился как `{"name": str, "arguments": object}` →
    эмитим `LLMToolCallBegin` + `LLMToolCallArgumentDelta` + новый
    `LLMGenerationDone(finish_reason=TOOL_CALLS)`. Агрегатор уровнем выше
    сам соберёт это в `ToolCallBlock` и положит в `LLMGenerationResult`.

  - не распарсился / не та форма → откатываемся: эмитим отложенный
    `LLMAnswerStarted`, потом весь буфер как один `LLMAnswerToken`,
    потом оригинальный `LLMGenerationDone` без подмен.

Состояние держится per-request_id, как у `AssistantAggregator`.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any
from uuid import uuid4

from boba.llm.events import (
    FinishReason,
    LLMAnswerStarted,
    LLMAnswerToken,
    LLMEvent,
    LLMGenerationDone,
    LLMGenerationStarted,
    LLMToolCallArgumentDelta,
    LLMToolCallBegin,
)
from boba.llm.models import LLMContext, RequestId
from boba.patterns import StreamSource

__all__ = ["JsonContentToolCallMiddleware"]


class _Mode(Enum):
    INITIAL = auto()
    BUFFERING = auto()
    PASSTHROUGH = auto()


@dataclass
class _PerRequestState:
    mode: _Mode = _Mode.INITIAL
    buffer: list[str] = field(default_factory=list)
    pending_answer_started: LLMAnswerStarted | None = None


class JsonContentToolCallMiddleware(StreamSource[LLMContext, LLMEvent]):
    """JSON-в-content → синтез LLMToolCallBegin/ArgumentDelta deltas."""

    _OPEN_CHARS = frozenset("{[")

    def __init__(self, inner: StreamSource[LLMContext, LLMEvent]) -> None:
        self._inner = inner
        self._state: dict[RequestId, _PerRequestState] = defaultdict(_PerRequestState)

    def name(self) -> str:
        return f"JsonContentToolCall({self._inner.name()})"

    def reset(self) -> None:
        self._state.clear()
        self._inner.reset()

    def stream(self, ctx: LLMContext) -> Iterable[LLMEvent]:
        for event in self._inner.stream(ctx):
            match event:
                case LLMGenerationStarted(request_id=rid):
                    self._state.pop(rid, None)
                    yield event
                case LLMAnswerStarted(request_id=rid):
                    st = self._state[rid]
                    if st.mode is _Mode.PASSTHROUGH:
                        yield event
                    else:
                        st.pending_answer_started = event
                case LLMAnswerToken(request_id=rid):
                    yield from self._on_answer_token(event)
                case LLMGenerationDone(request_id=rid):
                    yield from self._on_done(rid, event)
                case _:
                    yield event

    def _on_answer_token(self, event: LLMAnswerToken) -> Iterator[LLMEvent]:
        st = self._state[event.request_id]

        if st.mode is _Mode.PASSTHROUGH:
            yield event
            return

        if st.mode is _Mode.BUFFERING:
            st.buffer.append(event.token)
            return

        stripped = event.token.lstrip()
        if stripped and stripped[0] in self._OPEN_CHARS:
            st.mode = _Mode.BUFFERING
            st.buffer.append(event.token)
            return

        # первый непустой символ — не JSON: уходим в passthrough,
        # эмитим отложенный Started (если был) и текущий токен.
        st.mode = _Mode.PASSTHROUGH
        if st.pending_answer_started is not None:
            yield st.pending_answer_started
            st.pending_answer_started = None
        yield event

    def _on_done(
        self,
        rid: RequestId,
        done: LLMGenerationDone,
    ) -> Iterator[LLMEvent]:
        st = self._state.pop(rid, None)
        if st is None or st.mode is not _Mode.BUFFERING:
            yield done
            return

        raw = "".join(st.buffer)
        parsed = self._try_parse_tool_call(raw)

        if parsed is None:
            # не tool-call: возвращаем отложенный Started + буфер как Answer.
            if st.pending_answer_started is not None:
                yield st.pending_answer_started
            if raw:
                yield LLMAnswerToken(request_id=rid, token=raw)
            yield done
            return

        name, arguments = parsed
        tool_call_id = f"call_{uuid4().hex}"
        yield LLMToolCallBegin(
            request_id=rid,
            index=0,
            tool_call_id=tool_call_id,
            tool_name=name,
        )
        yield LLMToolCallArgumentDelta(
            request_id=rid,
            index=0,
            tool_call_id=tool_call_id,
            tool_name=name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        )
        yield LLMGenerationDone(request_id=rid, finish_reason=FinishReason.TOOL_CALLS)

    @staticmethod
    def _try_parse_tool_call(raw: str) -> tuple[str, dict[str, Any]] | None:
        if not raw.strip():
            return None
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        name = obj.get("name")
        arguments = obj.get("arguments")
        if not isinstance(name, str) or not name:
            return None
        if not isinstance(arguments, dict):
            return None
        return name, arguments
