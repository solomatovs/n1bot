"""Классификация сырых openai/httpx исключений в LLMError."""

from __future__ import annotations

import httpx

import openai
from boba.llm.errors import (
    LLMAuthError,
    LLMConnectionError,
    LLMContextLengthError,
    LLMError,
    LLMInvalidRequestError,
    LLMProviderInternalError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUnknownError,
)
from boba.patterns import Converter

_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR_MIN = 500
_HTTP_SERVER_ERROR_MAX = 600


class OpenAIErrorConverter(Converter[Exception, LLMError]):
    """Сырые openai/httpx исключения -> доменные LLMError.

    Порядок case-веток отражает иерархию openai/httpx:
      * subclass-ветка идёт раньше своего parent'а (APITimeoutError до
        APIConnectionError; RateLimitError до APIStatusError);
      * специфический guard на status_code идёт раньше «голого» APIStatusError;
      * httpx.TimeoutException до httpx.RequestError; httpx.RequestError до
        httpx.HTTPError; httpx.HTTPError до fallback.
    """

    @staticmethod
    def _describe_openai(exc: openai.APIError) -> str:
        """
        str(exc) + ' (METHOD URL)'. Инвариант: openai.APIError всегда несёт .request
        """
        return f"{exc} ({exc.request.method} {exc.request.url})"

    @staticmethod
    def _describe_httpx_request(exc: httpx.RequestError) -> str:
        """
        str(exc) + ' (METHOD URL)'. Инвариант: httpx.RequestError всегда несёт .request
        """
        return f"{exc} ({exc.request.method} {exc.request.url})"

    @staticmethod
    def _is_context_length(exc: openai.BadRequestError) -> bool:
        """openai.BadRequestError со специфическим маркером context_length_exceeded."""
        body = exc.body
        if isinstance(body, dict):
            if body.get("code") == "context_length_exceeded":
                return True
            err = body.get("error")
            if isinstance(err, dict) and err.get("code") == "context_length_exceeded":
                return True
        msg = str(exc).lower()
        return (
            "context length" in msg
            or "maximum context" in msg
            or "context window" in msg
            or "contextwindowexceeded" in msg
        )

    def convert(self, value: Exception) -> LLMError:  # noqa: C901, PLR0911, PLR0912
        match value:
            case openai.APITimeoutError() as e:
                return LLMTimeoutError(self._describe_openai(e))
            case openai.APIConnectionError() as e:
                return LLMConnectionError(self._describe_openai(e))
            case openai.RateLimitError() as e:
                return LLMRateLimitError(
                    self._describe_openai(e), status_code=e.status_code
                )
            case openai.AuthenticationError() | openai.PermissionDeniedError() as e:
                return LLMAuthError(self._describe_openai(e), status_code=e.status_code)
            case openai.BadRequestError() as e if self._is_context_length(e):
                return LLMContextLengthError(
                    self._describe_openai(e),
                    status_code=e.status_code,
                )
            case openai.BadRequestError() | openai.NotFoundError() as e:
                return LLMInvalidRequestError(
                    self._describe_openai(e),
                    status_code=e.status_code,
                )
            case openai.InternalServerError() as e:
                return LLMProviderInternalError(
                    self._describe_openai(e),
                    status_code=e.status_code,
                )
            case openai.APIStatusError() as e if (
                _HTTP_SERVER_ERROR_MIN <= e.status_code < _HTTP_SERVER_ERROR_MAX
            ):
                return LLMProviderInternalError(
                    self._describe_openai(e),
                    status_code=e.status_code,
                )
            case openai.APIStatusError() as e if (
                e.status_code == _HTTP_TOO_MANY_REQUESTS
            ):
                return LLMRateLimitError(
                    self._describe_openai(e), status_code=e.status_code
                )
            case openai.APIStatusError() as e:
                return LLMInvalidRequestError(
                    self._describe_openai(e),
                    status_code=e.status_code,
                )
            # httpx.TimeoutException ⊂ httpx.RequestError — обе несут .request.
            case httpx.TimeoutException() as e:
                return LLMTimeoutError(self._describe_httpx_request(e))
            case httpx.RequestError() as e:
                return LLMConnectionError(self._describe_httpx_request(e))
            # httpx.HTTPError без .request (InvalidURL, CookieConflict) —
            # дотягиваться до URL некуда, оставляем голый текст.
            case httpx.HTTPError() as e:
                return LLMConnectionError(str(e))
            case _:
                return LLMUnknownError(f"{type(value).__name__}: {value}")
