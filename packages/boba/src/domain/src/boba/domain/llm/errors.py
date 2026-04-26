"""Ошибки LLM-слоя.

    LLMError (Exception)
    ├── RetryableLLMError          имеет смысл повторить
    │   ├── LLMConnectionError
    │   ├── LLMTimeoutError
    │   ├── LLMRateLimitError              + status_code: int
    │   └── LLMProviderInternalError       + status_code: int
    └── PermanentLLMError          повтор не поможет
        ├── LLMAuthError                   + status_code: int
        ├── LLMInvalidRequestError         + status_code: int (HTTP 400)
        ├── LLMContextLengthError          + status_code: int
        ├── LLMProtocolError               ответ вне схемы
        ├── LLMRequestValidationError      клиентская валидация (без HTTP)
        │   ├── LLMRequestModelNoneError
        │   ├── LLMRequestEmptyMessagesError
        │   └── LLMRequestSystemMessageNoneError
        └── LLMUnknownError                нераспознанное исключение провайдера
"""

from __future__ import annotations


class LLMError(Exception):
    """
    Базовая ошибка обращения к LLM
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class RetryableLLMError(LLMError):
    """
    Маркер «имеет смысл повторить»: сеть, таймаут, rate-limit, 5xx
    Если подключен Middleware который обрабатывает эту ошибку
    То он по своей доступной логике может повторить http request в openai api
    """


class PermanentLLMError(LLMError):
    """Маркер «повтор не поможет»: auth, bad request, context length."""


class LLMConnectionError(RetryableLLMError):
    """Не удалось установить соединение (DNS, refused, reset)."""


class LLMTimeoutError(RetryableLLMError):
    """Превышен таймаут запроса/чтения ответа."""


class LLMRateLimitError(RetryableLLMError):
    """Провайдер ответил 429 Too Many Requests."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMProviderInternalError(RetryableLLMError):
    """Провайдер ответил 5xx или оборвал стрим по внутренней причине."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMUnknownError(PermanentLLMError):
    """
    Нераспознанное исключение провайдера

    Возвращается fallback-веткой OpenAIErrorConverter, когда
    ни одна из известных спецификаций не сматчилась. Консервативно
    помечаем как retryable — повторная попытка безопасна, а реальная
    классификация должна быть добавлена в правила конвертера.
    """


class LLMAuthError(PermanentLLMError):
    """Провайдер ответил 401/403."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMInvalidRequestError(PermanentLLMError):
    """Провайдер ответил 400 — запрос сформирован некорректно."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMContextLengthError(PermanentLLMError):
    """Суммарная длина сообщений превысила окно модели"""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMProtocolError(PermanentLLMError):
    """
    Провайдер вернул ответ вне ожидаемой схемы
    """


class LLMRequestValidationError(PermanentLLMError):
    """
    Клиентская валидация LLM-запроса упала

    Запрос не отправляется провайдеру — проверка запускается в
    finalize. HTTP-статуса у такой ошибки нет.
    """


class LLMRequestModelNoneError(LLMRequestValidationError):
    """
    LLM-запрос собран без model
    """

    def __init__(self) -> None:
        super().__init__("LLMRequest.model is None")


class LLMRequestEmptyMessagesError(LLMRequestValidationError):
    """
    LLM-запрос собран с пустым списком сообщений
    """

    def __init__(self) -> None:
        super().__init__("LLMRequest.messages is empty")


class LLMRequestSystemMessageNoneError(LLMRequestValidationError):
    """
    LLM-запрос собран без system_message
    """

    def __init__(self) -> None:
        super().__init__("LLMRequest.system_message is None")
