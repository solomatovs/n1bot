"""Извлечение reasoning-токена из ChoiceDelta по списку известных ключей."""

from __future__ import annotations

from boba.domain.core.patterns import Converter
from openai.types.chat.chat_completion_chunk import ChoiceDelta


class MultiKeyReasoningExtractor(Converter[ChoiceDelta, str | None]):
    """Извлекает reasoning-токен из delta.model_extra, перебирая
    известные ключи по порядку.

    Разные провайдеры кладут «рассуждения» модели в разные поля:

    - reasoning_content — DeepSeek, xAI Grok, часть OpenAI-compat прокси;
    - thinking — Anthropic через openai-compat, некоторые LiteLLM-маршруты;
    - reasoning — Ollama native, Groq.

    Дефолтный набор покрывает всех. Можно сузить/переопределить список,
    передав свой кортеж в конструктор.

    Провайдер-специфичный экстрактор — это просто другой
    Converter[ChoiceDelta, str | None] в отдельном модуле,
    подключается через DI параметром ThinkingSource /
    MetricsChatCompletionObserver.
    """

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
