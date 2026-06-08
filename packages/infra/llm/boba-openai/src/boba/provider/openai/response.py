"""Консьюмеры ответа OpenAI: ChatCompletionChunk (stream) и ChatCompletion."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterable, Iterator
from uuid import uuid4

from pydantic import BaseModel

from boba.llm.errors import LLMProtocolError
from boba.llm.events import (
    FinishReason,
    LLMAnswerDelta,
    LLMAnswerMessage,
    LLMEvent,
    LLMRefusalDelta,
    LLMRefusalMessage,
    LLMThinkingDelta,
    LLMThinkingMessage,
    LLMToolCallDecodeFailedMessage,
    LLMToolCallDelta,
    LLMToolCallMessage,
    LLMTotalMessage,
)
from boba.llm.models import (
    AssistantMessage,
    AssistantMessageChunk,
    LLMContext,
    RequestId,
    ToolCall,
    ToolCallDecodeFailure,
)
from boba.patterns import Converter, StreamTransformer
from openai.types.chat import ChatCompletion, ChatCompletionMessageFunctionToolCall
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    Choice,
    ChoiceDeltaToolCall,
)

__all__ = ["ToolCallFromContentFallback"]


logger = logging.getLogger(__name__)


class ToolCallFromContentFallback:
    """
    Перемапивает tool-call из content в tool_calls итогового сообщения.

    Чинит проблему когда в поле content приходит JSON по протоколу (function + args)
    """

    def decode(self, message: AssistantMessage, *, model: str) -> AssistantMessage:
        """Исправленное сообщение, либо тот же объект если это не tool-call."""
        if (
            message.tool_calls
            or message.tool_call_decode_failures
            or not message.content
        ):
            # если поля уже заполнены, или content пустой, то не трогаем сообщение
            return message

        calls = self._parse(message.content)
        if calls is None:
            return message

        logger.warning(
            "provider returned tool call(s) in content; remapped %d call(s) "
            "[%s] model=%s",
            len(calls),
            ", ".join(c.name for c in calls),
            model,
        )
        return message.model_copy(update={"content": "", "tool_calls": tuple(calls)})

    @classmethod
    def _parse(cls, content: str) -> list[ToolCall] | None:
        """Распарсить content по протоколу; None если это не tool-call."""
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return None

        items = parsed if isinstance(parsed, list) else [parsed]
        if not items:
            return None

        calls: list[ToolCall] = []
        for item in items:
            call = cls._to_call(item)
            if call is None:
                # Хотя бы один элемент не по протоколу — это не tool-call.
                return None
            calls.append(call)
        return calls

    @classmethod
    def _to_call(cls, item: object) -> ToolCall | None:
        """Один элемент {function, args} -> ToolCall; None если не по протоколу."""
        if not isinstance(item, dict):
            return None

        function = item.get("function")
        args = item.get("args")
        if not isinstance(function, str):
            return None

        if not isinstance(args, dict):
            return None

        return ToolCall(id=cls._new_call_id(), name=function, args=args)

    @staticmethod
    def _new_call_id() -> str:
        """Свежий tool_call_id — провайдер его не прислал."""
        return f"call_{uuid4().hex}"


class ChatCompletionChunkConsumer(
    StreamTransformer[LLMContext, ChatCompletionChunk, LLMEvent]
):
    """
    Консьюмер ответа OpenAI в режиме stream=True.

    Преобразует поток ChatCompletionChunk в доменные LLMEvent: на лету эмитит
    delta-события (thinking/answer/refusal/tool_call) и параллельно копит их в
    AssistantMessageChunk. Текстовые *Message-снапшоты закрываются инлайн, по
    переходу к следующему виду контента (пришёл content -> закрыли thinking и т.д.).
    Тул-снапшоты и LLMTotalMessage (терминатор + источник истины) — на
    finish_reason: в OpenAI args тула стримятся до самого конца.

    Последовательность видов — деталь OpenAI-стрима; наружу как гарантия не
    протекает, каждое *Message самодостаточно.
    """

    def __init__(
        self,
        request_id: RequestId,
        fallback: ToolCallFromContentFallback | None = None,
    ) -> None:
        self._request_id = request_id
        self._fallback = fallback
        self._reasoning = MultiKeyReasoningExtractor()
        self._finish = FinishReasonDecoder()
        # Чинит коллизии index у параллельных tool_calls до их сборки.
        self._reindexer = DuplicateToolCallIndexReindexer()
        # index -> накопитель tool-call'а (id, name, фрагменты args).
        self._tool_calls: dict[int, _PartialToolCall] = {}
        self._message = AssistantMessageChunk.empty()
        # Имя модели из чанков — только для лога fallback'а.
        self._model = ""
        # Текстовые слоты закрываются по переходу; флаги гасят повторный снапшот.
        self._thinking_flushed = False
        self._answer_flushed = False
        self._refusal_flushed = False
        # Провайдер может прислать два чанка с finish_reason (второй несёт
        # usage); без флага _finalize отработает дважды и продублирует
        # ToolCallMessage + TotalMessage.
        self._finalized = False

    def name(self) -> str:
        return "ChatCompletionChunkConsumer"

    def reset(self) -> None:
        self._reindexer.reset()
        self._tool_calls.clear()
        self._message = AssistantMessageChunk.empty()
        self._model = ""
        self._thinking_flushed = False
        self._answer_flushed = False
        self._refusal_flushed = False
        self._finalized = False

    def stream(
        self, ctx: LLMContext, stream: Iterable[ChatCompletionChunk]
    ) -> Iterable[LLMEvent]:
        for chunk in stream:
            self._model = chunk.model
            # Reindexer исправляет index у tool call
            # при параллельном вызове нескольких tool.
            for choice in self._reindexer.stream(ctx, chunk.choices):
                # Порядок: thinking -> answer -> refusal -> tool_calls -> finish.
                delta = choice.delta

                thinking = self._reasoning.convert(delta)
                if thinking:
                    self._message.append_thinking(thinking)
                    yield LLMThinkingDelta(request_id=self._request_id, token=thinking)

                if delta.content:
                    # переход thinking -> answer: закрываем thinking-слот
                    yield from self._flush_thinking()
                    self._message.append_text(delta.content)
                    yield LLMAnswerDelta(
                        request_id=self._request_id, token=delta.content
                    )

                if delta.refusal:
                    # переход -> refusal: закрываем thinking/answer
                    yield from self._flush_thinking()
                    yield from self._flush_answer()
                    self._message.append_refusal(delta.refusal)
                    yield LLMRefusalDelta(
                        request_id=self._request_id, token=delta.refusal
                    )

                if delta.tool_calls:
                    # переход -> tool_calls: закрываем все текстовые слоты
                    yield from self._flush_thinking()
                    yield from self._flush_answer()
                    yield from self._flush_refusal()
                    yield from self._tool_call_deltas(delta.tool_calls)

                if choice.finish_reason:
                    yield from self._finalize(choice.finish_reason)

    def _tool_call_deltas(
        self, tool_calls: Iterable[ChoiceDeltaToolCall]
    ) -> Iterator[LLMEvent]:
        for tc in tool_calls:
            first = tc.index not in self._tool_calls
            if first:
                if not (tc.id and tc.function and tc.function.name):
                    raise LLMProtocolError(
                        f"ToolCallDelta: первая дельта index={tc.index} без id/name"
                    )

                self._tool_calls[tc.index] = _PartialToolCall(tc.id, tc.function.name)

            partial = self._tool_calls[tc.index]
            arguments = (tc.function.arguments if tc.function else None) or ""
            if arguments:
                partial.append_args(arguments)

            # Первая дельта эмитится всегда (id+name)
            # последующие — только при непустом фрагменте args.
            if first or arguments:
                yield LLMToolCallDelta(
                    request_id=self._request_id,
                    index=tc.index,
                    tool_call_id=partial.id,
                    tool_name=partial.name,
                    arguments=arguments,
                )

    def _flush_thinking(self) -> Iterator[LLMEvent]:
        if self._thinking_flushed:
            return
        self._thinking_flushed = True
        content = self._message.thinking
        if content:
            yield LLMThinkingMessage(request_id=self._request_id, content=content)

    def _flush_answer(self) -> Iterator[LLMEvent]:
        if self._answer_flushed:
            return
        self._answer_flushed = True
        content = self._message.text
        if content:
            yield LLMAnswerMessage(request_id=self._request_id, content=content)

    def _flush_refusal(self) -> Iterator[LLMEvent]:
        if self._refusal_flushed:
            return
        self._refusal_flushed = True
        content = self._message.refusal
        if content:
            yield LLMRefusalMessage(request_id=self._request_id, content=content)

    def _finalize(self, finish_reason: str) -> Iterator[LLMEvent]:
        if self._finalized:
            return
        self._finalized = True
        reason = self._finish.convert(finish_reason)

        # Fallback: провайдер вернул вызов текстом в content, нативный
        # tool_calls пуст — перемапим и закроем без текстового answer.
        remapped = self._try_fallback()
        if remapped is not None:
            yield from self._flush_thinking()
            yield from self._flush_refusal()
            for call in remapped.tool_calls:
                yield LLMToolCallMessage(request_id=self._request_id, call=call)
            yield LLMTotalMessage(
                request_id=self._request_id,
                message=remapped,
                finish_reason=reason,
            )
            return

        # Закрываем незакрытые текстовые слоты (если перехода к ним не было).
        yield from self._flush_thinking()
        yield from self._flush_answer()
        yield from self._flush_refusal()
        # Тул-снапшоты — пачкой на finish: в OpenAI args стримятся до конца.
        for index in sorted(self._tool_calls):
            call = self._tool_calls[index].decode()
            self._message.add_tool_call(call)
            if isinstance(call, ToolCall):
                yield LLMToolCallMessage(request_id=self._request_id, call=call)
            else:
                yield LLMToolCallDecodeFailedMessage(
                    request_id=self._request_id, failure=call
                )
        # Терминатор генерации + источник истины для реконструкции истории.
        yield LLMTotalMessage(
            request_id=self._request_id,
            message=self._message.finalize(),
            finish_reason=reason,
        )

    def _try_fallback(self) -> AssistantMessage | None:
        """Исправленное сообщение, если content оказался tool-call'ом; иначе None.

        Только при пустом нативном tool_calls (self._tool_calls): иначе
        провайдер вызов прислал штатно и content трогать нельзя.
        """
        if self._fallback is None or self._tool_calls:
            return None
        message = self._message.finalize()
        remapped = self._fallback.decode(message, model=self._model)
        return remapped if remapped is not message else None


class ChatCompletionConsumer:
    """
    Консьюмер ответа OpenAI в режиме stream=False.

    Собирает готовый ChatCompletion в AssistantMessage и эмитит только итоговые
    события (*Complete + LLMTotalMessage) — без delta-событий.
    """

    def __init__(
        self,
        request_id: RequestId,
        fallback: ToolCallFromContentFallback | None = None,
    ) -> None:
        self._request_id = request_id
        self._fallback = fallback
        self._reasoning = MultiKeyReasoningExtractor()
        self._finish = FinishReasonDecoder()

    def consume(self, response: ChatCompletion) -> Iterator[LLMEvent]:
        if not response.choices:
            raise LLMProtocolError("ChatCompletion without choices")

        choice = response.choices[0]
        message = choice.message
        chunk = AssistantMessageChunk.empty()

        thinking = self._reasoning.convert(message)
        if thinking:
            chunk.append_thinking(thinking)

        if message.content:
            chunk.append_text(message.content)

        if message.refusal:
            chunk.append_refusal(message.refusal)

        for tc in message.tool_calls or ():
            if not isinstance(tc, ChatCompletionMessageFunctionToolCall):
                raise LLMProtocolError(
                    f"tool_call {tc.id}: ожидался function-tool, пришёл {tc.type}"
                )

            partial = _PartialToolCall(tc.id, tc.function.name)
            partial.append_args(tc.function.arguments or "")
            chunk.add_tool_call(partial.decode())

        reason = self._finish.convert(choice.finish_reason)

        message = chunk.finalize()
        if self._fallback is not None:
            message = self._fallback.decode(message, model=response.model)

        yield from self._emit_snapshots(message, reason)

    def _emit_snapshots(
        self, message: AssistantMessage, reason: FinishReason
    ) -> Iterator[LLMEvent]:
        """Развернуть готовый AssistantMessage в итоговые *Message + result."""
        if message.thinking:
            yield LLMThinkingMessage(
                request_id=self._request_id, content=message.thinking
            )
        if message.content:
            yield LLMAnswerMessage(request_id=self._request_id, content=message.content)
        if message.refusal:
            yield LLMRefusalMessage(
                request_id=self._request_id, content=message.refusal
            )
        for call in message.tool_calls:
            yield LLMToolCallMessage(request_id=self._request_id, call=call)
        for failure in message.tool_call_decode_failures:
            yield LLMToolCallDecodeFailedMessage(
                request_id=self._request_id, failure=failure
            )
        yield LLMTotalMessage(
            request_id=self._request_id, message=message, finish_reason=reason
        )


class _PartialToolCall:
    """Накопитель одного tool-call из стрим-фрагментов + декод args.

    id+name приходят на первой дельте; фрагменты args — строкой. decode()
    парсит args в dict -> ToolCall, либо при битом JSON / не-объекте возвращает
    ToolCallDecodeFailure с сырьём и причиной (ошибка как значение, не исключение).
    Декод args — провайдерская работа: OpenAI отдаёт args строкой, поэтому парс
    живёт здесь, а домен хранит уже готовое значение.
    """

    def __init__(self, tool_call_id: str, tool_name: str) -> None:
        self.id = tool_call_id
        self.name = tool_name
        self._args = io.StringIO()

    def append_args(self, fragment: str) -> None:
        self._args.write(fragment)

    def decode(self) -> ToolCall | ToolCallDecodeFailure:
        raw = self._args.getvalue()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError as e:
            return ToolCallDecodeFailure(
                id=self.id,
                name=self.name,
                raw=raw,
                error=f"invalid JSON arguments: {e}",
            )
        if not isinstance(parsed, dict):
            return ToolCallDecodeFailure(
                id=self.id,
                name=self.name,
                raw=raw,
                error=f"args must be JSON object, got {type(parsed).__name__}",
            )
        return ToolCall(id=self.id, name=self.name, args=parsed)


class FinishReasonDecoder(Converter[str, FinishReason]):
    """Разбирает raw finish_reason провайдера в доменный FinishReason."""

    def convert(self, value: str) -> FinishReason:
        try:
            return FinishReason(value)
        except ValueError as e:
            raise LLMProtocolError(
                f"unknown finish_reason from provider: {value!r}"
            ) from e


class MultiKeyReasoningExtractor(Converter[BaseModel, str | None]):
    """
    Этот класс позволяет обойти проблему того, что
    разные модели эмитят thinking в разные поля
    извлечение thinking происходит в одном из найденных полей в порядке приоритета:
    - reasoning_content
    - thinking
    - reasoning

    Работает и над ChoiceDelta (stream), и над ChatCompletionMessage (non-stream)
    — обоим достаточно доступа к model_extra.
    """

    DEFAULT_KEYS: tuple[str, ...] = (
        "reasoning_content",
        "thinking",
        "reasoning",
    )

    def __init__(self, keys: tuple[str, ...] | None = None) -> None:
        self._keys = keys if keys is not None else self.DEFAULT_KEYS

    def convert(self, value: BaseModel) -> str | None:
        extra = value.model_extra or {}
        for k in self._keys:
            v = extra.get(k)
            if v:
                return str(v)
        return None


class DuplicateToolCallIndexReindexer(StreamTransformer[LLMContext, Choice, Choice]):
    """
    Перемапливает index поле tool_calls на свободные места
    Это нужно если llm случайно прислала один и тот же index для нескольких tool_call
    Мы внучную исправляем index и далее все компоненты системы думают,
    что index пришел корректный. Далее при ответе в llm отправляются эти индексы
    как будто бы она вызвала этот набор toolcall'ов с этими индексами

    Пример. Провайдер шлёт два вызова, оба index=0:
        {index:0, id:A, name:…} -> слот 0 свободен -> A index=0
        {index:0, id:B, name:…} -> слот 0 занят A, B проставляется index=1
    """

    def __init__(self) -> None:
        self._index_owner: dict[int, str] = {}
        self._remap: dict[str, int] = {}
        self._next_free: int = 0

    def name(self) -> str:
        return "DuplicateToolCallIndexReindexer"

    def reset(self) -> None:
        self._index_owner.clear()
        self._remap.clear()
        self._next_free = 0

    def stream(self, ctx: LLMContext, stream: Iterable[Choice]) -> Iterable[Choice]:
        for choice in stream:
            delta = choice.delta
            if delta is not None and delta.tool_calls:
                for tc in delta.tool_calls:
                    self._rewrite(tc)

            yield choice

    def _rewrite(self, tc: ChoiceDeltaToolCall) -> None:
        original = tc.index
        tc_id = tc.id
        if not tc_id:
            owner = self._index_owner.get(original)
            if owner is not None and owner in self._remap:
                tc.index = self._remap[owner]
            return
        if tc_id in self._remap:
            tc.index = self._remap[tc_id]
            return
        owner = self._index_owner.get(original)
        if owner is None:
            self._index_owner[original] = tc_id
            if original >= self._next_free:
                self._next_free = original + 1
            return
        if owner != tc_id:
            self._assign_new_index(tc, tc_id, original)

    def _assign_new_index(
        self, tc: ChoiceDeltaToolCall, tc_id: str, original: int
    ) -> None:
        new_index = self._next_free
        self._next_free += 1
        self._index_owner[new_index] = tc_id
        self._remap[tc_id] = new_index
        tc.index = new_index
        logger.info(
            "tool_call index collision: id=%s remapped %d -> %d",
            tc_id,
            original,
            new_index,
        )
