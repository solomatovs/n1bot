"""OpenAI-совместимая реализация LLM — terminal и middleware."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx
import openai
from openai import OpenAI
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionToolParam,
    ChatCompletionUserMessageParam,
)
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    Choice,
)

from boba.adapters.raw_llm_observer import RawLLMObserver
from boba.domain.agent.errors import (
    LLMAuthError,
    LLMConnectionError,
    LLMContextLengthError,
    LLMError,
    LLMInvalidRequestError,
    LLMProviderInternalError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from boba.domain.agent.events import (
    AgentEvent,
    AnswerStarted,
    AnswerToken,
    GenerationDone,
    GenerationStarted,
    RefusalToken,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
)
from boba.domain.agent.llm_request_factory import LLMRequestFactory
from boba.domain.agent.models import (
    AgentContext,
    LLMMessage,
    LLMRequest,
    RequestId,
    SamplingParams,
)
from boba.domain.config import LLMConfig
from boba.domain.core.errors import Retryable
from boba.domain.core.patterns import (
    Converter,
    ExceptionSpecification,
    IsInstance,
    StreamConverter,
    StreamSource,
    StreamTransformer,
    StreamTransformerPipeline,
)
from boba.domain.core.tools import Tool

logger = logging.getLogger(__name__)


class LoggingLLMMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Логирует запрос, количество событий и время генерации."""

    def __init__(self, inner: StreamSource[AgentContext, AgentEvent]) -> None:
        self._inner = inner

    def name(self) -> str:
        return "LoggingLLM"

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        logger.info("LLM request: model=%s", ctx.request.model)
        start = time.monotonic()
        count = 0

        for event in self._inner.stream(ctx):
            count += 1
            yield event

        elapsed = time.monotonic() - start
        logger.info("LLM done: %d events in %.2fs", count, elapsed)


class StupidRetryLLMMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Повторяет запрос при любой ошибке с маркером :class:`Retryable`
    до ``max_retries`` раз.

    Ловит по **маркеру**, не по конкретному типу — это позволяет добавлять
    новые повторяемые ошибки (``RetryablePromptError``, …) без правки
    retry-слоя. Не-``Retryable`` исключения (``PermanentLLMError``,
    программные баги) проходят насквозь сразу.
    """

    def __init__(
        self, inner: StreamSource[AgentContext, AgentEvent], max_retries: int = 3
    ) -> None:
        self._inner = inner
        self._max_retries = max_retries

    def name(self) -> str:
        return "RetryLLM"

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        for attempt in range(self._max_retries):
            try:
                yield from self._inner.stream(ctx)
                return
            except Retryable as e:
                if attempt == self._max_retries - 1:
                    raise
                logger.warning(
                    "LLM attempt %d/%d failed, retrying: %s: %s",
                    attempt + 1,
                    self._max_retries,
                    type(e).__name__,
                    e,
                )


class FromOpenAIChunkConverter(
    StreamTransformer[AgentContext, ChatCompletionChunk, AgentEvent]
):
    """
    Конвертирует поток OpenAI chunks в поток AgentEvent.
    Делегирует обработку подключаемым StreamTransformer-ам через pipeline.
    """

    def __init__(self, request_id: RequestId) -> None:
        self._pipeline = StreamTransformerPipeline[AgentContext, Choice, AgentEvent](
            [
                RoleSource(request_id),
                ThinkingSource(request_id),
                AnswerSource(request_id),
                RefusalSource(request_id),
                ToolCallSource(request_id),
                FinishSource(request_id),
            ]
        )

    def name(self) -> str:
        return f"FromOpenAIChunkConverter({self._pipeline.name()})"

    def stream(
        self, ctx: AgentContext, stream: Iterable[ChatCompletionChunk]
    ) -> Iterable[AgentEvent]:
        for chunk in stream:
            yield from self._pipeline.stream(ctx, chunk.choices)


class OpenAIMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    Terminal — вызывает OpenAI-совместимый API.
    Получает готовый :class:`LLMRequest` от :class:`LLMRequestFactory`,
    мапит его в kwargs провайдера через :class:`ToOpenAIRequestConverter`
    и стримит :class:`AgentEvent`.
    """

    def __init__(
        self,
        config: LLMConfig,
        llm_request_factory: LLMRequestFactory,
        observer: RawLLMObserver,
    ) -> None:
        self._client = OpenAI(base_url=config.base_url, api_key=config.api_key)
        self._llm_request_factory = llm_request_factory
        self._observer = observer
        self._to_request_converter = ToOpenAIRequestConverter()
        self._error_converter = OpenAIErrorConverter()

    def name(self) -> str:
        return "OpenAICompletion"

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        llm_request = self._llm_request_factory.build(ctx)
        kwargs = self._to_request_converter.convert(llm_request)

        try:
            self._observer.on_request(kwargs)
            response = self._client.chat.completions.create(**kwargs)
            yield from FromOpenAIChunkConverter(ctx.request.request_id).stream(
                ctx, self._log_chunks(response)
            )
        except LLMError:
            raise
        except (openai.APIError, httpx.HTTPError) as e:
            raise self._error_converter.convert(e) from e

    def _log_chunks(
        self, chunks: Iterable[ChatCompletionChunk]
    ) -> Iterable[ChatCompletionChunk]:
        for chunk in chunks:
            self._observer.on_response_chunk(chunk)
            yield chunk


class IsContextLengthError(ExceptionSpecification):
    """Матчит ``openai.BadRequestError`` с маркером ``context_length_exceeded``
    в теле ответа либо в текстовом сообщении (для OpenAI-совместимых бэкендов,
    которые не всегда возвращают структурированный ``code``).
    """

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
        return "context length" in msg or "maximum context" in msg


class IsServerError(ExceptionSpecification):
    """Матчит ``openai.APIStatusError`` с 5xx-статусом."""

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
    """Матчит ``openai.APIStatusError`` с конкретным HTTP-статусом."""

    def __init__(self, code: int) -> None:
        self._code = code

    def check(self, candidate: Exception) -> bool:
        return (
            isinstance(candidate, openai.APIStatusError)
            and candidate.status_code == self._code
        )


@dataclass(frozen=True)
class ErrorRule:
    """Правило классификации: предикат + строитель доменной ошибки."""

    match: ExceptionSpecification
    build: Callable[[Exception], LLMError]


class OpenAIErrorConverter(Converter[Exception, LLMError]):
    """Классифицирует сырые ``openai``/``httpx`` исключения в доменные
    :class:`LLMError` по списку правил.

    Правила применяются в порядке регистрации; первое совпадение — победитель.
    Если ни одно не сработало — fallback в :class:`LLMProviderInternalError`.
    Без аргумента используется стандартный набор правил
    (см. :meth:`default_rules`).
    """

    _HTTP_TOO_MANY_REQUESTS = 429

    def __init__(self, rules: list[ErrorRule] | None = None) -> None:
        self._rules = rules if rules is not None else self.default_rules()

    def convert(self, value: Exception) -> LLMError:
        for rule in self._rules:
            if rule.match.check(value):
                return rule.build(value)
        return LLMProviderInternalError(f"{type(value).__name__}: {value}")

    @staticmethod
    def status_code(exc: Exception) -> int | None:
        """HTTP-код из ``openai.APIStatusError`` либо ``None``."""
        return exc.status_code if isinstance(exc, openai.APIStatusError) else None

    @classmethod
    def default_rules(cls) -> list[ErrorRule]:
        """Стандартный набор правил для OpenAI-совместимых провайдеров.

        Порядок важен: подклассы должны идти раньше родителей
        (``APITimeoutError`` → ``APIConnectionError``; спец-случай
        ``context_length_exceeded`` → общий ``BadRequestError``).
        """
        sc = cls.status_code
        return [
            ErrorRule(
                IsInstance(openai.APITimeoutError),
                lambda e: LLMTimeoutError(str(e)),
            ),
            ErrorRule(
                IsInstance(openai.APIConnectionError),
                lambda e: LLMConnectionError(str(e)),
            ),
            ErrorRule(
                IsInstance(openai.RateLimitError),
                lambda e: LLMRateLimitError(str(e), status_code=sc(e)),
            ),
            ErrorRule(
                IsInstance(openai.AuthenticationError, openai.PermissionDeniedError),
                lambda e: LLMAuthError(str(e), status_code=sc(e)),
            ),
            ErrorRule(
                IsInstance(openai.BadRequestError).and_(IsContextLengthError()),
                lambda e: LLMContextLengthError(str(e), status_code=sc(e)),
            ),
            ErrorRule(
                IsInstance(openai.BadRequestError, openai.NotFoundError),
                lambda e: LLMInvalidRequestError(str(e), status_code=sc(e)),
            ),
            ErrorRule(
                IsInstance(openai.InternalServerError).or_(IsServerError()),
                lambda e: LLMProviderInternalError(str(e), status_code=sc(e)),
            ),
            ErrorRule(
                IsStatusCode(cls._HTTP_TOO_MANY_REQUESTS),
                lambda e: LLMRateLimitError(str(e), status_code=sc(e)),
            ),
            ErrorRule(
                IsInstance(openai.APIStatusError),
                lambda e: LLMInvalidRequestError(str(e), status_code=sc(e)),
            ),
            ErrorRule(
                IsInstance(httpx.TimeoutException),
                lambda e: LLMTimeoutError(str(e)),
            ),
            ErrorRule(
                IsInstance(httpx.HTTPError),
                lambda e: LLMConnectionError(str(e)),
            ),
        ]


class ToOpenAIRequestConverter(Converter[LLMRequest, dict[str, Any]]):
    """Мапит :class:`LLMRequest` в kwargs для
    ``client.chat.completions.create``.
    """

    def __init__(self) -> None:
        self._to_message_converter = ToOpenAIMessageConverter()
        self._to_tool_converter = ToOpenAIToolConverter()

    def convert(self, value: LLMRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": value.model,
            "messages": list(self._to_message_converter.convert(value.messages)),
            "stream": True,
        }

        if value.tools:
            kwargs["tools"] = [self._to_tool_converter.convert(t) for t in value.tools]
        if value.tool_choice is not None:
            kwargs["tool_choice"] = value.tool_choice
        if value.response_format is not None:
            kwargs["response_format"] = value.response_format
        if value.parallel_tool_calls is not None:
            kwargs["parallel_tool_calls"] = value.parallel_tool_calls

        self._apply_sampling(kwargs, value.sampling)
        return kwargs

    @staticmethod
    def _apply_sampling(kwargs: dict[str, Any], s: SamplingParams) -> None:
        fields: dict[str, Any] = {
            "temperature": s.temperature,
            "top_p": s.top_p,
            "max_tokens": s.max_tokens,
            "seed": s.seed,
            "stop": s.stop,
            "frequency_penalty": s.frequency_penalty,
            "presence_penalty": s.presence_penalty,
        }
        for key, val in fields.items():
            if val is not None:
                kwargs[key] = val


class ToOpenAIToolConverter(Converter[Tool[Any], ChatCompletionToolParam]):
    """Конвертирует Tool в формат OpenAI tools API."""

    def convert(self, value: Tool[Any]) -> ChatCompletionToolParam:
        definition = value.definition()

        properties: dict[str, dict[str, str]] = {}
        required: list[str] = []

        for p in definition.input_schema.params:
            properties[p.name] = {
                "type": p.type.value,
                "description": p.description,
            }
            if p.required:
                required.append(p.name)

        return {
            "type": "function",
            "function": {
                "name": value.tool_id().to_wire(),
                "description": definition.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }


class ToOpenAIOneMessageConverter(Converter[LLMMessage, ChatCompletionMessageParam]):
    """Конвертирует LLMMessage в формат OpenAI API."""

    def convert(self, value: LLMMessage) -> ChatCompletionMessageParam:
        match value.role:
            case "system":
                return ChatCompletionSystemMessageParam(
                    role="system",
                    content=value.content,
                )
            case "user":
                return ChatCompletionUserMessageParam(
                    role="user",
                    content=value.content,
                )
            case "assistant":
                param = ChatCompletionAssistantMessageParam(
                    role="assistant",
                    content=value.content,
                )
                if value.tool_calls:
                    param["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments},
                        }
                        for tc in value.tool_calls
                    ]
                return param
            case "tool":
                return ChatCompletionToolMessageParam(
                    role="tool",
                    content=value.content,
                    tool_call_id=value.tool_call_id or "",
                )
            case _:
                raise ValueError(f"Unknown message role: {value.role}")


class ToOpenAIMessageConverter(StreamConverter[LLMMessage, ChatCompletionMessageParam]):
    """Конвертирует LLMMessage в формат OpenAI API."""

    def __init__(self) -> None:
        self._converter = ToOpenAIOneMessageConverter()

    def set_request_id(self, request_id: RequestId) -> None:
        self._request_id = request_id

    def convert(
        self, stream: Iterable[LLMMessage]
    ) -> Iterable[ChatCompletionMessageParam]:
        for item in stream:
            yield self._converter.convert(item)


class RoleSource(StreamTransformer[AgentContext, Choice, AgentEvent]):
    """Порождает GenerationStarted при первом появлении роли."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id
        self._started = False

    def name(self) -> str:
        return "Role"

    def reset(self) -> None:
        self._started = False

    def stream(
        self, ctx: AgentContext, stream: Iterable[Choice]
    ) -> Iterable[AgentEvent]:
        if not self._started:
            for choice in stream:
                if choice.delta.role and not self._started:
                    self._started = True
                    yield GenerationStarted(request_id=self._request_id)


class ThinkingSource(StreamTransformer[AgentContext, Choice, AgentEvent]):
    """Порождает ThinkingStarted/ThinkingToken из reasoning_content."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id
        self._started = False

    def name(self) -> str:
        return "Thinking"

    def reset(self) -> None:
        self._started = False

    def stream(
        self, ctx: AgentContext, stream: Iterable[Choice]
    ) -> Iterable[AgentEvent]:
        for choice in stream:
            extra = choice.delta.model_extra or {}
            thinking = extra.get("reasoning_content") or extra.get("thinking")

            if thinking:
                if not self._started:
                    self._started = True
                    yield ThinkingStarted(request_id=self._request_id)

                yield ThinkingToken(request_id=self._request_id, token=thinking)


class AnswerSource(StreamTransformer[AgentContext, Choice, AgentEvent]):
    """Порождает AnswerStarted/AnswerToken из content."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id
        self._started = False

    def name(self) -> str:
        return "Answer"

    def reset(self) -> None:
        self._started = False

    def stream(
        self, ctx: AgentContext, stream: Iterable[Choice]
    ) -> Iterable[AgentEvent]:
        for choice in stream:
            if choice.delta.content:
                if not self._started:
                    self._started = True
                    yield AnswerStarted(request_id=self._request_id)
                yield AnswerToken(
                    request_id=self._request_id, token=choice.delta.content
                )


class RefusalSource(StreamTransformer[AgentContext, Choice, AgentEvent]):
    """Порождает RefusalToken из refusal."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id

    def name(self) -> str:
        return "Refusal"

    def stream(
        self, ctx: AgentContext, stream: Iterable[Choice]
    ) -> Iterable[AgentEvent]:
        for choice in stream:
            if choice.delta.refusal:
                yield RefusalToken(
                    request_id=self._request_id, token=choice.delta.refusal
                )


class ToolCallSource(StreamTransformer[AgentContext, Choice, AgentEvent]):
    """Порождает ToolCallBegin/ToolCallArgumentDelta из tool_calls."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id
        self._seen: set[int] = set()

    def name(self) -> str:
        return "ToolCall"

    def reset(self) -> None:
        self._seen.clear()

    def stream(
        self, ctx: AgentContext, stream: Iterable[Choice]
    ) -> Iterable[AgentEvent]:
        for choice in stream:
            if not choice.delta.tool_calls:
                continue
            for tc in choice.delta.tool_calls:
                if (
                    tc.index not in self._seen
                    and tc.id
                    and tc.function
                    and tc.function.name
                ):
                    self._seen.add(tc.index)
                    yield ToolCallBegin(
                        request_id=self._request_id,
                        index=tc.index,
                        tool_call_id=tc.id,
                        tool_name=tc.function.name,
                    )
                if tc.function and tc.function.arguments:
                    yield ToolCallArgumentDelta(
                        request_id=self._request_id,
                        index=tc.index,
                        arguments=tc.function.arguments,
                    )


class FinishSource(StreamTransformer[AgentContext, Choice, AgentEvent]):
    """Порождает GenerationDone при finish_reason."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id

    def name(self) -> str:
        return "Finish"

    def stream(
        self, ctx: AgentContext, stream: Iterable[Choice]
    ) -> Iterable[AgentEvent]:
        for choice in stream:
            if choice.finish_reason:
                yield GenerationDone(
                    request_id=self._request_id,
                    finish_reason=choice.finish_reason,
                )
