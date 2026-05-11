"""Извлечение reasoning-токена из ChoiceDelta по списку известных ключей."""

from __future__ import annotations

from boba.patterns import Converter
from openai.types.chat.chat_completion_chunk import ChoiceDelta


class MultiKeyReasoningExtractor(Converter[ChoiceDelta, str | None]):
    """Извлекает reasoning-токен из delta.model_extra по списку ключей."""

    DEFAULT_KEYS: tuple[str, ...] = (
        "reasoning_content",
        "thinking",
        "reasoning",
    )

    def __init__(self, keys: tuple[str, ...] | None = None) -> None:
        self._keys = keys if keys is not None else self.DEFAULT_KEYS

    def convert(self, value: ChoiceDelta) -> str | None:
        extra = value.model_extra or {}
        for k in self._keys:
            v = extra.get(k)
            if v:
                return str(v)
        return None
