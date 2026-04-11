"""Парсинг <think>...</think> тегов в потоке токенов.

Используется при стриминге ответа LLM для разделения
размышлений (DeepSeek, QwQ) и финального ответа.
"""
from __future__ import annotations

from enum import Enum
from typing import Iterator, NamedTuple


class FragmentRole(Enum):
    THINKING = "thinking"
    ANSWER = "answer"


class ThinkFragment(NamedTuple):
    role: FragmentRole
    text: str


class ThinkTagParser:
    """Stateful парсер <think>...</think> тегов в потоке токенов."""

    _TAG_OPEN = "<think>"
    _TAG_CLOSE = "</think>"

    def __init__(self) -> None:
        self._in_think = False

    def feed(self, token: str) -> Iterator[ThinkFragment]:
        """Разобрать токен на фрагменты thinking/answer."""
        buf = token
        while buf:
            if self._in_think:
                end = buf.find(self._TAG_CLOSE)
                if end != -1:
                    yield ThinkFragment(FragmentRole.THINKING, buf[:end])
                    buf = buf[end + len(self._TAG_CLOSE):]
                    self._in_think = False
                else:
                    yield ThinkFragment(FragmentRole.THINKING, buf)
                    buf = ""
            else:
                start = buf.find(self._TAG_OPEN)
                if start != -1:
                    if start > 0:
                        yield ThinkFragment(FragmentRole.ANSWER, buf[:start])
                    buf = buf[start + len(self._TAG_OPEN):]
                    self._in_think = True
                else:
                    yield ThinkFragment(FragmentRole.ANSWER, buf)
                    buf = ""
