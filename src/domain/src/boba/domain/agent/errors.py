"""Доменная иерархия ошибок LLM.

Адаптеры ловят сырые исключения провайдера (``openai.*``, ``httpx.*``,
``httpcore.*``) и реиспускают их как эти типы. Выше по стеку код работает
только с доменными типами — retry-middleware матчит ``RetryableLLMError``,
клиенты читают ``GenerationFailed``-событие, потребители-SDK подписываются
на базовый ``LLMError``.
"""

from __future__ import annotations


class LLMError(Exception):
    """Базовая доменная ошибка обращения к LLM."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RetryableLLMError(LLMError):
    """Ошибка, которую имеет смысл повторить: сеть, таймаут, rate-limit, 5xx."""


class PermanentLLMError(LLMError):
    """Ошибка, которую повторять бессмысленно: auth, bad request, context length."""


class LLMConnectionError(RetryableLLMError):
    """Не удалось установить соединение с провайдером (DNS, refused, reset)."""


class LLMTimeoutError(RetryableLLMError):
    """Превышен таймаут запроса/чтения ответа."""


class LLMRateLimitError(RetryableLLMError):
    """Провайдер ответил 429 Too Many Requests."""


class LLMProviderInternalError(RetryableLLMError):
    """Провайдер ответил 5xx или оборвал стрим по внутренней причине."""


class LLMAuthError(PermanentLLMError):
    """Провайдер ответил 401/403 — неверный или отозванный ключ."""


class LLMInvalidRequestError(PermanentLLMError):
    """Провайдер ответил 400 — запрос сформирован некорректно."""


class LLMContextLengthError(PermanentLLMError):
    """Суммарная длина сообщений превысила окно модели."""


class LLMResponseFormatError(PermanentLLMError):
    """Ответ провайдера не удалось распарсить в ожидаемую структуру."""
