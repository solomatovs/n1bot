"""Починка коллизии ``index`` у параллельных tool_calls в стриме.

Некоторые OpenAI-совместимые провайдеры (в частности, Ollama через
LiteLLM) при параллельных вызовах инструментов эмитят несколько
полностью сформированных tool_call-дельт, все с одинаковым
``index`` (обычно ``0``), но с разными ``id``. По спецификации OpenAI
streaming ``index`` — это стабильный идентификатор слота, и
одинаковый ``index`` означает «это продолжение того же tool_call».
В результате downstream-агрегатор склеивает ``arguments`` разных
вызовов в одну строку (``{"path": "/a"}{"path": "/b"}``), и JSON-парсер
падает с ``Extra data``.

Этот StreamTransformer вклинивается до ``ToolCallSource`` и правит
коллизию: если приходит чанк с новым ``tool_call_id`` на уже занятом
``index``, ему присваивается первый свободный слот. Дальше по пайплайну
всё выглядит как нормальные параллельные вызовы, оба инструмента
доходят до исполнения.

Модуль подключается опционально — см. ``OpenAIMiddleware
.chunk_preprocessor_factory``. Убрать из цепочки — удалить одну строку
в DI.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from boba.domain.core.patterns import StreamTransformer
from boba.domain.llm.models import LLMContext
from openai.types.chat.chat_completion_chunk import Choice, ChoiceDeltaToolCall

logger = logging.getLogger(__name__)


class DuplicateToolCallIndexReindexer(StreamTransformer[LLMContext, Choice, Choice]):
    """Перемапливает tool_calls с коллизией по ``index`` на свободные слоты.

    Работает в LLM-слое (ниже agent'а) — параметризован
    :class:`LLMContext`, а не :class:`AgentContext`: препроцессор
    сидит внутри :class:`OpenAITerminal`'s chunk-pipeline, до
    любой agent-семантики.
    """

    def __init__(self) -> None:
        self._index_owner: dict[int, str] = {}
        self._remap: dict[str, int] = {}
        self._next_free: int = 0

    def name(self) -> str:
        return "DuplicateToolCallIndexReindexer"

    def reset(self) -> None:
        self._index_owner.clear()
        self._remap.clear()
        self._next_free = 0

    def stream(self, ctx: LLMContext, stream: Iterable[Choice]) -> Iterable[Choice]:
        for choice in stream:
            delta = choice.delta
            if delta is not None and delta.tool_calls:
                for tc in delta.tool_calls:
                    self._rewrite(tc)
            yield choice

    def _rewrite(self, tc: ChoiceDeltaToolCall) -> None:
        original = tc.index
        tc_id = tc.id
        if not tc_id:
            owner = self._index_owner.get(original)
            if owner is not None and owner in self._remap:
                tc.index = self._remap[owner]
            return
        if tc_id in self._remap:
            tc.index = self._remap[tc_id]
            return
        owner = self._index_owner.get(original)
        if owner is None:
            self._index_owner[original] = tc_id
            if original >= self._next_free:
                self._next_free = original + 1
            return
        if owner != tc_id:
            self._assign_new_index(tc, tc_id, original)

    def _assign_new_index(
        self, tc: ChoiceDeltaToolCall, tc_id: str, original: int
    ) -> None:
        new_index = self._next_free
        self._next_free += 1
        self._index_owner[new_index] = tc_id
        self._remap[tc_id] = new_index
        tc.index = new_index
        logger.info(
            "tool_call index collision: id=%s remapped %d -> %d",
            tc_id,
            original,
            new_index,
        )
