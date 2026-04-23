"""OpenAI-совместимая реализация LLM — terminal и middleware."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
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
    ChoiceDelta,
)

from boba.adapters.raw_llm_observer import (
    MultiKeyReasoningExtractor,
    RawLLMObserver,
)
from boba.domain.agent.errors import (
    LLMAuthError,
    LLMConnectionError,
    LLMContextLengthError,
    LLMError,
    LLMInvalidRequestError,
    LLMProtocolError,
    LLMProviderInternalError,
    LLMRateLimitError,
    LLMRequestModelNoneError,
    LLMTimeoutError,
)
from boba.domain.agent.events import (
    AgentEvent,
    AnswerStarted,
    AnswerToken,
    FinishReason,
    GenerationDone,
    GenerationStarted,
    LLMRequestSent,
    RefusalToken,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
)

# from boba.domain.agent.llm_request_factory import LLMRequestFactory
from boba.domain.agent.models import (
    AgentContext,
    LLMMessage,
    LLMRequestBuilder,
    RequestId,
)
from boba.domain.config import LLMConfig
from boba.domain.core.errors import Retryable
from boba.domain.core.patterns import (
    Converter,
    ExceptionSpecification,
    FirstMatchConverter,
    IsInstance,
    Specification,
    StreamConverter,
    StreamSource,
    StreamTransformer,
    StreamTransformerPipeline,
)
from boba.domain.core.tools import Tool

logger = logging.getLogger(__name__)


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

    ``chunk_preprocessors`` — опциональная цепочка трансформеров уровня
    ``Choice → Choice``, выполняемая последовательно ДО fan-out pipeline.
    Используется для нормализации кривых дельт провайдера (например,
    коллизий ``index`` у параллельных tool_calls), не замусоривая
    основную логику источников событий.
    """

    def __init__(
        self,
        request_id: RequestId,
        chunk_preprocessors: Sequence[
            StreamTransformer[AgentContext, Choice, Choice]
        ] = (),
    ) -> None:
        self._pipeline = StreamTransformerPipeline[AgentContext, Choice, AgentEvent](
            [
                RoleSource(request_id),
                ThinkingSource(request_id, MultiKeyReasoningExtractor()),
                AnswerSource(request_id),
                RefusalSource(request_id),
                ToolCallSource(request_id),
                FinishSource(request_id),
            ]
        )
        self._preprocessors: tuple[
            StreamTransformer[AgentContext, Choice, Choice], ...
        ] = tuple(chunk_preprocessors)

    def name(self) -> str:
        return f"FromOpenAIChunkConverter({self._pipeline.name()})"

    def stream(
        self, ctx: AgentContext, stream: Iterable[ChatCompletionChunk]
    ) -> Iterable[AgentEvent]:
        for chunk in stream:
            choices: Iterable[Choice] = chunk.choices
            for pre in self._preprocessors:
                choices = pre.stream(ctx, choices)
            yield from self._pipeline.stream(ctx, list(choices))


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
        observer: RawLLMObserver,
        chunk_preprocessor_factory: Callable[
            [RequestId],
            Sequence[StreamTransformer[AgentContext, Choice, Choice]],
        ] = lambda _rid: (),
    ) -> None:
        self._client = OpenAI(base_url=config.base_url, api_key=config.api_key)
        self._observer = observer
        self._to_request_converter = ToOpenAIRequestConverter()
        self._error_converter = OpenAIErrorConverter()
        self._chunk_preprocessor_factory = chunk_preprocessor_factory

    def name(self) -> str:
        return "OpenAICompletion"

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        kwargs = self._to_request_converter.convert(ctx.llm_request)

        outcome = "ok"
        self._observer.on_request(kwargs)
        try:
            try:
                response = self._client.chat.completions.create(**kwargs)

                yield LLMRequestSent(
                    request_id=ctx.agent_request.request_id,
                    model=str(kwargs.get("model", "")),
                    messages_count=len(kwargs.get("messages") or ()),
                    has_tools=bool(kwargs.get("tools")),
                )
                preprocessors = self._chunk_preprocessor_factory(
                    ctx.agent_request.request_id
                )
                yield from FromOpenAIChunkConverter(
                    ctx.agent_request.request_id, preprocessors
                ).stream(ctx, self._observe_chunks(response))
            except LLMError:
                raise
            except (openai.APIError, httpx.HTTPError) as e:
                raise self._error_converter.convert(e) from e
        except GeneratorExit:
            outcome = "cancelled"
            raise
        except BaseException as e:
            outcome = f"raised:{type(e).__name__}"
            raise
        finally:
            self._observer.on_request_end(outcome)

    def _observe_chunks(
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
        # Разные бэкенды формулируют по-своему:
        # OpenAI: "maximum context length"
        # Anthropic/Azure: "context length"
        # LiteLLM pre-call check: "context window exceeded"
        # vLLM: "token limit"
        return (
            "context length" in msg
            or "maximum context" in msg
            or "context window" in msg
            or "contextwindowexceeded" in msg
        )


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


class OpenAIErrorConverter(FirstMatchConverter[Exception, LLMError]):
    """Классифицирует сырые ``openai``/``httpx`` исключения в доменные
    :class:`LLMError` через :class:`FirstMatchConverter`.

    Правила применяются в порядке регистрации; первое совпадение —
    победитель. Если ни одно не сработало — fallback в
    :class:`LLMProviderInternalError`. Без аргумента используется
    стандартный набор правил (см. :meth:`default_rules`).
    """

    _HTTP_TOO_MANY_REQUESTS = 429

    def __init__(
        self,
    ) -> None:
        super().__init__(
            routes=self.default_rules(),
            fallback_route=lambda e: LLMProviderInternalError(
                f"{type(e).__name__}: {e}"
            ),
        )

    @staticmethod
    def status_code(exc: Exception) -> int | None:
        """HTTP-код из ``openai.APIStatusError`` либо ``None``."""
        return exc.status_code if isinstance(exc, openai.APIStatusError) else None

    @classmethod
    def default_rules(
        cls,
    ) -> list[tuple[Specification[Exception], Callable[[Exception], LLMError]]]:
        """Стандартный набор правил для OpenAI-совместимых провайдеров.

        Порядок важен: подклассы должны идти раньше родителей
        (``APITimeoutError`` → ``APIConnectionError``; спец-случай
        ``context_length_exceeded`` → общий ``BadRequestError``).
        """
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


class ToOpenAIRequestConverter(Converter[LLMRequestBuilder, dict[str, Any]]):
    """Мапит :class:`LLMRequest` в kwargs для
    ``client.chat.completions.create``.
    """

    def __init__(self) -> None:
        self._to_message_converter = ToOpenAIMessageConverter()
        self._to_tool_converter = ToOpenAIToolConverter()

    def convert(self, value: LLMRequestBuilder) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "stream": True,
        }

        self._apply_model(kwargs, value)
        self._apply_messages(kwargs, value)
        self._apply_sampling(kwargs, value)
        self._apply_tools(kwargs, value)
        self._apply_response_format(kwargs, value)

        return kwargs

    def _apply_model(self, kwargs: dict[str, Any], s: LLMRequestBuilder) -> None:
        if not s.model:
            raise LLMRequestModelNoneError()

        kwargs.update(
            {
                "model": s.model,
            }
        )

    def _apply_messages(self, kwargs: dict[str, Any], s: LLMRequestBuilder) -> None:
        kwargs.update(
            {
                "messages": s.messages,
            }
        )

    def _apply_sampling(self, kwargs: dict[str, Any], s: LLMRequestBuilder) -> None:
        fields: dict[str, Any] = {
            "temperature": s.sampling.temperature,
            "top_p": s.sampling.top_p,
            "max_tokens": s.sampling.max_tokens,
            "seed": s.sampling.seed,
            "stop": s.sampling.stop,
            "frequency_penalty": s.sampling.frequency_penalty,
            "presence_penalty": s.sampling.presence_penalty,
        }

        for key, val in fields.items():
            if val is not None:
                kwargs[key] = val

    def _apply_tools(self, kwargs: dict[str, Any], value: LLMRequestBuilder) -> None:
        if value.tools.tools:
            kwargs["tools"] = [
                self._to_tool_converter.convert(t) for t in value.tools.tools
            ]
        if value.tools.tool_choice is not None:
            kwargs["tool_choice"] = value.tools.tool_choice
        if value.tools.parallel_tool_calls is not None:
            kwargs["parallel_tool_calls"] = value.tools.parallel_tool_calls

    def _apply_response_format(self, kwargs: dict[str, Any], value: LLMRequestBuilder):
        if value.response_format is not None:
            kwargs["response_format"] = value.response_format


class ToOpenAIToolConverter(Converter[Tool[Any], ChatCompletionToolParam]):
    """Конвертирует Tool в формат OpenAI tools API.

    Описание каждого параметра собирается через
    :meth:`ParamSchema.build_wire_schema` — обходит валидатор
    (:class:`SchemaContributor`) и заполняет ``type``/``enum``/``default``/
    флаг required. Дрейфа со схемой валидации быть не может: оба
    канала читают из одного :class:`Validator`.
    """

    def convert(self, value: Tool[Any]) -> ChatCompletionToolParam:
        definition = value.definition()

        properties: dict[str, dict[str, Any]] = {}
        required: list[str] = []

        for p in definition.input_schema.params:
            wire = p.build_wire_schema()
            properties[p.name] = wire.property
            if wire.required:
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
    """Порождает ``ThinkingStarted``/``ThinkingToken``.

    Извлечение reasoning-поля делегируется
    ``Converter[ChoiceDelta, str | None]``. По умолчанию —
    :class:`MultiKeyReasoningExtractor` на все известные провайдеры;
    для нестандартного бэкенда передай кастомный Converter через DI.
    """

    def __init__(
        self,
        request_id: RequestId,
        extractor: Converter[ChoiceDelta, str | None],
    ) -> None:
        self._request_id = request_id
        self._extractor = extractor
        self._started = False

    def name(self) -> str:
        return "Thinking"

    def reset(self) -> None:
        self._started = False

    def stream(
        self, ctx: AgentContext, stream: Iterable[Choice]
    ) -> Iterable[AgentEvent]:
        for choice in stream:
            thinking = self._extractor.convert(choice.delta)
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
        self._saw_tool_call = False

    def name(self) -> str:
        return "Finish"

    def reset(self) -> None:
        self._saw_tool_call = False

    def stream(
        self, ctx: AgentContext, stream: Iterable[Choice]
    ) -> Iterable[AgentEvent]:
        for choice in stream:
            if choice.delta.tool_calls:
                self._saw_tool_call = True
            if choice.finish_reason:
                try:
                    reason = FinishReason(choice.finish_reason)
                except ValueError as e:
                    raise LLMProtocolError(
                        f"unknown finish_reason from provider: {choice.finish_reason!r}"
                    ) from e
                # Ollama /api/chat в стриме отдаёт finish_reason=stop даже
                # когда в дельтах были native tool_calls. Без подмены
                # StopOnFinished завершит цикл до исполнения инструмента.
                if self._saw_tool_call and reason is not FinishReason.TOOL_CALLS:
                    reason = FinishReason.TOOL_CALLS
                yield GenerationDone(
                    request_id=self._request_id,
                    finish_reason=reason,
                )
