"""Порт чата с моделью: конверт запроса, события ответа, ошибка слоя.

Чат-модель — транспорт «сообщения и инструменты -> поток ответа». История,
исполнение инструментов и ход — забота вызывающего слоя. Ответ по схеме —
режим того же запроса: reply_schema объявляет форму ответа, а как её навязать
модели, знает реализация (функция с tool_choice, format, грамматика).

Ошибки:
LlmError — модель не загрузилась, провайдер недоступен, ответил не по
    контракту или оборвал генерацию (фильтр, поток без финала); поток событий
    обрывается этой ошибкой.
LlmOutputLimitError — подкласс LlmError: ответ срезан пределом длины (finish_reason
    length, done_reason length, расход потолка локального прогона).
LlmContextOverflowError — подкласс LlmError: запрос не поместился в окно модели
    (ответ 4xx или ошибка сервера с текстом о длине контекста).
LlmServiceError — подкласс LlmError: провайдер занят, недоступен, отказал или
    оборвал ответ; вид беды (LlmTrouble) и короткая причина — для человека.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from enum import StrEnum
from http import HTTPStatus
from typing import Any, ClassVar, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.core.errors import PublicError

__all__ = [
    "ChatDelta",
    "ChatEvent",
    "ChatImage",
    "ChatModel",
    "ChatReply",
    "ChatRequest",
    "ChatRole",
    "ChatTurn",
    "ChatUsage",
    "ContextOverflowSigns",
    "LlmConnectionFailure",
    "LlmContextOverflowError",
    "LlmError",
    "LlmErrorDetails",
    "LlmErrors",
    "LlmOutputLimitError",
    "LlmServiceError",
    "LlmTrouble",
    "ToolCall",
    "ToolSpec",
]


class LlmError(PublicError):
    """Обращение к модели не состоялось: загрузка, сеть, статус, мусорный ответ."""

    def __init__(self, diagnostic: str) -> None:
        super().__init__(
            diagnostic,
            "The model request failed. Try again or contact the administrator.",
        )


class LlmTrouble(StrEnum):
    """Что случилось с провайдером модели — по этому виду человек получает
    понятный текст, а не ошибку транспорта."""

    BUSY = "busy"
    """Провайдер просит подождать: 429."""

    DOWN = "down"
    """Провайдер не отвечает: соединение, срок, 5xx."""

    REJECTED = "rejected"
    """Провайдер отказал в запросе: прочие 4xx."""

    CUT = "cut"
    """Ответ оборвался или кончился не по правилам."""

    BLOCKED = "blocked"
    """Ответ закрыт фильтром содержимого провайдера."""

    EMPTY = "empty"
    """Модель ответила пустотой: ни текста, ни вызовов."""

    UNKNOWN = "unknown"
    """Обращение не состоялось по иной причине: подробности — в журнале."""


class LlmErrors:
    """Граница порта модели: журнал диагностики и упаковка неизвестного сбоя.

    HTTP-модели и реестр провайдеров держат эту границу на всё время операции,
    включая чтение потока; отмена задачи остаётся сигналом управления.
    """

    def __init__(self) -> None:
        self._logger = logging.getLogger(__name__)

    @contextmanager
    def boundary(self, action: str, *, report: bool = True) -> Iterator[None]:
        try:
            yield
        except LlmError as exc:
            if report:
                self._logger.error("%s: %s", action, exc.diagnostic, exc_info=True)

            raise
        except Exception as exc:
            message = f"{action}: {type(exc).__name__}: {exc}"
            if report:
                self._logger.exception("%s", message)

            raise LlmError(message) from exc


class LlmConnectionFailure(StrEnum):
    """Допустимые публичные причины отказа соединения с моделью."""

    NO_CONNECTION = "no connection"
    NO_ANSWER = "no answer in time"
    CERTIFICATE = "the certificate check failed"
    BROKE_OFF = "the connection broke off"
    REFUSED = "the server refused the request"


class LlmErrorDetails:
    """Публичная причина по HTTP-статусу или закрытому перечню отказов сети.

    Провайдеры формируют новые сообщения, ACP применяет тот же перечень к
    сохранённым ошибкам при воспроизведении истории.
    """

    STATUS: ClassVar[re.Pattern[str]] = re.compile(r"^HTTP ([1-5][0-9]{2})(?:\s|$)")

    def status(self, status: int) -> str:
        try:
            return f"HTTP {status} {HTTPStatus(status).phrase}"
        except ValueError:
            return f"HTTP {status}"

    def public(self, detail: str) -> str:
        if match := self.STATUS.match(detail):
            return self.status(int(match.group(1)))

        try:
            return LlmConnectionFailure(detail).value
        except ValueError:
            return ""


class LlmServiceError(LlmError):
    """Отказ модели с видом сбоя и HTTP-статусом без данных подключения."""

    def __init__(
        self,
        diagnostic: str,
        trouble: LlmTrouble,
        *,
        status: int | None = None,
        reason: LlmConnectionFailure | None = None,
    ) -> None:
        messages = {
            LlmTrouble.BUSY: "The model service is busy. Try again later.",
            LlmTrouble.DOWN: "The model service is unavailable. Try again later.",
            LlmTrouble.REJECTED: (
                "The model service rejected the request. Contact the administrator."
            ),
            LlmTrouble.CUT: "The model response was interrupted. Try again.",
            LlmTrouble.BLOCKED: (
                "The model service blocked the response with its content filter."
            ),
            LlmTrouble.EMPTY: "The model returned an empty response. Try again.",
            LlmTrouble.UNKNOWN: "The model request failed. Contact the administrator.",
        }
        self.detail = ""
        if reason is not None:
            self.detail = reason.value

        if status is not None:
            self.detail = LlmErrorDetails().status(status)

        message = messages[trouble]
        if self.detail:
            message = f"{message} ({self.detail})"

        PublicError.__init__(self, diagnostic, message)
        self.trouble = trouble


class LlmOutputLimitError(LlmError):
    """Ответ оборван пределом длины: модель замолчала на полуслове."""

    def __init__(self, diagnostic: str) -> None:
        PublicError.__init__(
            self, diagnostic, "The model response reached its output token limit."
        )


class LlmContextOverflowError(LlmError):
    """Запрос не поместился в окно модели."""

    def __init__(self, diagnostic: str) -> None:
        PublicError.__init__(
            self, diagnostic, "The request exceeds the model's context window."
        )
        self.excess = ContextOverflowSigns().excess(diagnostic)


class ContextOverflowSigns:
    """Распознаёт по тексту ошибки сервера, что запрос не поместился в окно.

    Единого кода у совместимых серверов нет: OpenAI отвечает
    `context_length_exceeded`, vLLM и llama.cpp — своим текстом про длину
    контекста. Реализации портов зовут этот класс для 4xx и для ошибок в теле.
    """

    PHRASES: ClassVar[tuple[str, ...]] = (
        "context_length_exceeded",
        "context length",
        "context window",
        "context size",
        "maximum context",
        "prompt is too long",
        "input is too long",
        "too many tokens",
        "exceeds the limit",
    )

    EXCESS: ClassVar[tuple[re.Pattern[str], ...]] = (
        re.compile(
            r"maximum context length is (?P<limit>\d+) tokens.*?"
            r"(?:resulted in|requested) (?P<actual>\d+) tokens",
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            r"prompt is too long[^0-9]*(?P<actual>\d+)\s*tokens?\s*>\s*(?P<limit>\d+)",
            re.IGNORECASE,
        ),
    )
    """Тексты, в которых сервер называет окно и размер запроса: OpenAI и
    vLLM («maximum context length is N tokens… resulted in / requested M»),
    Anthropic («prompt is too long: M tokens > N»)."""

    def matches(self, text: str) -> bool:
        return any(self._hits(text.lower()))

    def excess(self, text: str) -> int | None:
        """На сколько токенов запрос больше окна по тексту ошибки сервера;
        None — сервер чисел не назвал."""
        for pattern in self.EXCESS:
            found = pattern.search(text)
            if found is None:
                continue

            actual = int(found.group("actual"))
            limit = int(found.group("limit"))

            return max(0, actual - limit)

        return None

    def _hits(self, lowered: str) -> Iterator[bool]:
        for phrase in self.PHRASES:
            yield phrase in lowered


class ChatRole(StrEnum):
    """Роли сообщений диалога."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolSpec(BaseModel):
    """Объявление инструмента для модели: имя, описание, json schema аргументов.

    Та же модель описывает форму ответа по схеме (ChatRequest.reply_schema):
    удалённому провайдеру она уходит функцией, локальному — грамматикой.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    parameters: Mapping[str, Any]
    deferred: bool = Field(
        default=False,
        description=(
            "Отложенный инструмент, который модель загрузила поиском: ссылка на "
            "него пришла в результате инструмента (ChatTurn.tool_references). "
            "Провайдер с родными отложенными инструментами шлёт его с "
            "defer_loading; остальные — обычной записью в порядке списка: "
            "вызывающий ставит такие инструменты в конец, в порядке загрузки."
        ),
    )


class ToolCall(BaseModel):
    """Вызов инструмента в ответе модели: разобранные аргументы, не сырой json."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Идентификатор вызова; ответ едет с ним же.")
    name: str = Field(description="Имя инструмента.")
    arguments: Mapping[str, Any] = Field(description="Аргументы вызова.")


class ChatImage(BaseModel):
    """Картинка в сообщении пользователя: байты и их media type."""

    model_config = ConfigDict(frozen=True)

    media_type: str
    data: bytes


class ChatTurn(BaseModel):
    """Одно сообщение истории в конверте запроса. Текст — строкой content
    либо отдельными частями parts: так сборщик подаёт соседние сообщения
    пользователя, вложения агента и инструкцию пересказа, слитые в одно
    сообщение (в docs/prompt_assembly это отдельные текстовые блоки одного
    сообщения). Провайдер, который умеет части, шлёт их массивом, остальные
    — склейкой text()."""

    model_config = ConfigDict(frozen=True)

    PART_JOIN: ClassVar[str] = "\n"
    """Склейка частей для бэкенда без частей текста."""

    role: ChatRole
    content: str = ""
    parts: Sequence[str] = Field(
        default=(),
        description=(
            "Текст сообщения отдельными частями; пусто — текст одной строкой "
            "content. Заданы части — content пуст."
        ),
    )
    images: Sequence[ChatImage] = Field(
        default=(),
        description="Картинки сообщения пользователя; бэкенд без зрения отказывает.",
    )
    reasoning: str | None = Field(
        default=None,
        description=(
            "Рассуждения ассистента, которые провайдер возвращает модели. "
            "None — поля у сообщения не было, бэкенду оно не отправляется; "
            "пустая строка отправляется как есть: провайдер в режиме "
            "размышления требует поле у каждого сообщения ассистента."
        ),
    )
    tool_calls: Sequence[ToolCall] = Field(
        default=(),
        description="Вызовы инструментов сообщения ассистента.",
    )
    tool_call_id: str = Field(
        default="",
        description="Для роли tool: вызов, на который отвечает сообщение.",
    )
    tool_references: Sequence[str] = Field(
        default=(),
        description=(
            "Для роли tool: имена инструментов, которые результат загрузил "
            "(ссылки tool_reference). content уже называет их текстом; "
            "провайдер с родными ссылками шлёт их блоками tool_reference."
        ),
    )

    @model_validator(mode="after")
    def _one_text(self) -> ChatTurn:
        if self.parts and self.content:
            msg = (
                f"A {self.role.value} message carries both content and parts; "
                "the text goes either as one string or as parts"
            )
            raise ValueError(msg)

        return self

    def text_parts(self) -> Sequence[str]:
        """Части текста: заданные либо content одной частью; пустой текст —
        ни одной."""
        if self.parts:
            return self.parts

        if self.content:
            return (self.content,)

        return ()

    def text(self) -> str:
        """Текст одной строкой: части через PART_JOIN."""
        return self.PART_JOIN.join(self.text_parts())


class ChatRequest(BaseModel):
    """Конверт запроса: сообщения, инструменты, форма ответа и сэмплинг."""

    model_config = ConfigDict(frozen=True)

    messages: Sequence[ChatTurn]
    tools: Sequence[ToolSpec] = ()
    reply_schema: ToolSpec | None = Field(
        default=None,
        description=(
            "Форма ответа: модель обязана ответить объектом по этой схеме. "
            "None — свободный ответ."
        ),
    )
    sampling: Mapping[str, Any] = Field(
        default_factory=dict,
        description=(
            "Параметры запроса к провайдеру как есть: ключи и значения уходят "
            "в тело без проверок; что принимает провайдер — решает конфиг."
        ),
    )
    stream: bool = Field(
        default=True,
        description=(
            "Просить у бэкенда дельты; False — один запрос-ответ без потока, "
            "бэкенд отдаёт только финальный ChatReply."
        ),
    )


class ChatDelta(BaseModel):
    """Потоковый кусок ответа: текст и рассуждения растут дельтами.

    Вызовы инструментов дельтами не отдаются: их аргументы копит реализация
    и отдаёт целиком в финальном ChatReply.
    """

    model_config = ConfigDict(frozen=True)

    content: str = ""
    reasoning: str = ""


class ChatUsage(BaseModel):
    """Учёт токенов обращения; нули — провайдер учёт не прислал."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = Field(
        default=0,
        description=(
            "Часть output_tokens, ушедшая в рассуждения; 0 — провайдер "
            "рассуждения отдельно не считает."
        ),
    )


class ChatReply(BaseModel):
    """Финальное сообщение ответа целиком; всегда последнее событие потока."""

    model_config = ConfigDict(frozen=True)

    content: str = ""
    reasoning: str = ""
    tool_calls: Sequence[ToolCall] = ()
    usage: ChatUsage = ChatUsage()


ChatEvent: TypeAlias = ChatDelta | ChatReply


class ChatModel(ABC):
    """Порт чата: конверт запроса -> поток событий.

    Поток заканчивается ровно одним ChatReply; нестримящий бэкенд отдаёт
    только его, без дельт.
    """

    @abstractmethod
    def chat(self, request: ChatRequest) -> AsyncIterator[ChatEvent]: ...

    async def reply(self, request: ChatRequest) -> ChatReply:
        """Финал ответа одним значением; поток без ChatReply — нарушение порта."""
        final: ChatReply | None = None
        async for event in self.chat(request):
            if isinstance(event, ChatReply):
                final = event

        if final is None:
            msg = "chat model ended the event stream without a ChatReply"
            raise LlmError(msg)

        return final
