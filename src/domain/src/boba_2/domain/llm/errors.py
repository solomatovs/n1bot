"""Ошибки LLM-слоя.

Независимая иерархия, не связанная с агентским слоем. Терминальный
адаптер (OpenAI/др.) конвертирует сырые исключения провайдера
(``openai.*``, ``httpx.*``) в эти типы; middleware LLM-слоя (retry,
cache) принимают решения, опираясь на базовые маркеры
(:class:`RetryableLLMError` / :class:`PermanentLLMError`). Выше по
стеку — на границе агента — перехватываются и превращаются в
агент-специфичные ``*Failed``-события.

Политика — **не наследуемся** от
``boba.domain.core.errors.Retryable`` / ``TerminalError``. Это
осознанная изоляция: LLM-слой не знает про агентские события и
feedback-каналы; связность только через точечные переходы на границе.

════════════════════════════════════════════════════════════════════
  Иерархия
════════════════════════════════════════════════════════════════════

::

    LLMError (Exception)
    │   status_code: int | None
    │
    ├── RetryableLLMError          маркер «имеет смысл повторить»
    │   ├── LLMConnectionError
    │   ├── LLMTimeoutError
    │   ├── LLMRateLimitError
    │   └── LLMProviderInternalError
    │
    └── PermanentLLMError          маркер «повтор не поможет»
        ├── LLMAuthError
        ├── LLMInvalidRequestError
        │   ├── LLMRequestModelNoneError
        │   └── LLMRequestUserMessageNoneError
        ├── LLMContextLengthError
        └── LLMProtocolError
"""

from __future__ import annotations


class LLMError(Exception):
    """
    Базовая ошибка обращения к LLM
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RetryableLLMError(LLMError):
    """
    Маркер «имеет смысл повторить»: сеть, таймаут, rate-limit, 5xx
    """


class PermanentLLMError(LLMError):
    """Маркер «повтор не поможет»: auth, bad request, context length."""


class LLMConnectionError(RetryableLLMError):
    """Не удалось установить соединение (DNS, refused, reset)."""


class LLMTimeoutError(RetryableLLMError):
    """Превышен таймаут запроса/чтения ответа."""


class LLMRateLimitError(RetryableLLMError):
    """Провайдер ответил 429 Too Many Requests."""


class LLMProviderInternalError(RetryableLLMError):
    """Провайдер ответил 5xx или оборвал стрим по внутренней причине."""


class LLMAuthError(PermanentLLMError):
    """Провайдер ответил 401/403."""


class LLMInvalidRequestError(PermanentLLMError):
    """Провайдер ответил 400 — запрос сформирован некорректно."""


class LLMContextLengthError(PermanentLLMError):
    """Суммарная длина сообщений превысила окно модели"""


class LLMProtocolError(PermanentLLMError):
    """
    Провайдер вернул ответ вне ожидаемой схемы
    """


class LLMRequestModelNoneError(LLMInvalidRequestError):
    """
    LLM-запрос собран без model
    """

    def __init__(self) -> None:
        super().__init__("LLMRequest.model is None")


class LLMRequestUserMessageNoneError(LLMInvalidRequestError):
    """
    LLM-запрос собран без user_message
    """

    def __init__(self) -> None:
        super().__init__("LLMRequest.user_message is None")
