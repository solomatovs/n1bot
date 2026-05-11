"""Классификация сырых openai/httpx исключений в LLMError."""

from __future__ import annotations

from collections.abc import Callable

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
from boba.patterns import (
    ExceptionSpecification,
    FirstMatchConverter,
    IsInstance,
    Specification,
)


class IsContextLengthError(ExceptionSpecification):
    """Матчит openai.BadRequestError с маркером context_length_exceeded."""

    def check(self, candidate: Exception) -> bool:
        if not isinstance(candidate, openai.BadRequestError):
            return False
        body = getattr(candidate, "body", None)
        if isinstance(body, dict):
            if body.get("code") == "context_length_exceeded":
                return True
            err = body.get("error")
            if isinstance(err, dict) and err.get("code") == "context_length_exceeded":
                return True
        msg = str(candidate).lower()
        return (
            "context length" in msg
            or "maximum context" in msg
            or "context window" in msg
            or "contextwindowexceeded" in msg
        )


class IsServerError(ExceptionSpecification):
    """Матчит openai.APIStatusError с 5xx."""

    _HTTP_SERVER_ERROR_MIN = 500
    _HTTP_SERVER_ERROR_MAX = 600

    def check(self, candidate: Exception) -> bool:
        if not isinstance(candidate, openai.APIStatusError):
            return False

        sc = candidate.status_code
        return (
            sc is not None
            and self._HTTP_SERVER_ERROR_MIN <= sc < self._HTTP_SERVER_ERROR_MAX
        )


class IsStatusCode(ExceptionSpecification):
    """Матчит openai.APIStatusError с конкретным HTTP-статусом."""

    def __init__(self, code: int) -> None:
        self._code = code

    def check(self, candidate: Exception) -> bool:
        return (
            isinstance(candidate, openai.APIStatusError)
            and candidate.status_code == self._code
        )


class OpenAIErrorConverter(FirstMatchConverter[Exception, LLMError]):
    """Сырые openai/httpx исключения → доменные LLMError."""

    _HTTP_TOO_MANY_REQUESTS = 429

    def __init__(self) -> None:
        super().__init__(
            routes=self.default_rules(),
            fallback_route=lambda e: LLMUnknownError(f"{type(e).__name__}: {e}"),
        )

    @staticmethod
    def status_code(exc: Exception) -> int:
        """HTTP-статус из APIStatusError."""
        if not isinstance(exc, openai.APIStatusError):  # pragma: no cover — инвариант
            raise RuntimeError(
                "OpenAIErrorConverter.status_code invariant broken: "
                f"expected APIStatusError, got {type(exc).__name__}"
            )
        return exc.status_code

    @classmethod
    def default_rules(
        cls,
    ) -> list[tuple[Specification[Exception], Callable[[Exception], LLMError]]]:
        sc = cls.status_code
        return [
            (
                IsInstance(openai.APITimeoutError),
                lambda e: LLMTimeoutError(str(e)),
            ),
            (
                IsInstance(openai.APIConnectionError),
                lambda e: LLMConnectionError(str(e)),
            ),
            (
                IsInstance(openai.RateLimitError),
                lambda e: LLMRateLimitError(str(e), status_code=sc(e)),
            ),
            (
                IsInstance(openai.AuthenticationError, openai.PermissionDeniedError),
                lambda e: LLMAuthError(str(e), status_code=sc(e)),
            ),
            (
                IsInstance(openai.BadRequestError).and_(IsContextLengthError()),
                lambda e: LLMContextLengthError(str(e), status_code=sc(e)),
            ),
            (
                IsInstance(openai.BadRequestError, openai.NotFoundError),
                lambda e: LLMInvalidRequestError(str(e), status_code=sc(e)),
            ),
            (
                IsInstance(openai.InternalServerError).or_(IsServerError()),
                lambda e: LLMProviderInternalError(str(e), status_code=sc(e)),
            ),
            (
                IsStatusCode(cls._HTTP_TOO_MANY_REQUESTS),
                lambda e: LLMRateLimitError(str(e), status_code=sc(e)),
            ),
            (
                IsInstance(openai.APIStatusError),
                lambda e: LLMInvalidRequestError(str(e), status_code=sc(e)),
            ),
            (
                IsInstance(httpx.TimeoutException),
                lambda e: LLMTimeoutError(str(e)),
            ),
            (
                IsInstance(httpx.HTTPError),
                lambda e: LLMConnectionError(str(e)),
            ),
        ]
