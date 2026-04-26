"""Наблюдатель сырых запросов/ответов LLM на границе OpenAI-клиента.

Позволяет сохранять kwargs ``chat.completions.create`` и каждый входящий
``ChatCompletionChunk`` в неизменённом виде — до любой доменной конверсии.
Используется для отладки, сбора датасетов, анализа поведения моделей через
Ollama/LiteLLM/третьи прокси.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.workspace import HistoryWorkspaceShell
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk, ChoiceDelta

logger = logging.getLogger(__name__)


class MultiKeyReasoningExtractor(Converter[ChoiceDelta, str | None]):
    """Извлекает reasoning-токен из ``delta.model_extra``, перебирая
    известные ключи по порядку.

    Разные провайдеры кладут «рассуждения» модели в разные поля:

    - ``reasoning_content`` — DeepSeek, xAI Grok, часть OpenAI-compat прокси;
    - ``thinking`` — Anthropic через openai-compat, некоторые LiteLLM-маршруты;
    - ``reasoning`` — Ollama native, Groq.

    Дефолтный набор покрывает всех. Можно сузить/переопределить список,
    передав свой кортеж в конструктор.

    Провайдер-специфичный экстрактор — это просто другой
    :class:`Converter[ChoiceDelta, str | None]` в отдельном модуле,
    подключается через DI параметром :class:`ThinkingSource` /
    :class:`MetricsRawLLMObserver`.
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


class RequestOutcomeKind(Enum):
    """Дискриминатор :class:`RequestOutcome`."""

    OK = "ok"
    CANCELLED = "cancelled"
    RAISED = "raised"


@dataclass(frozen=True, slots=True)
class RequestOutcome:
    """Исход запроса LLM на wire-слое.

    Инвариант: ``exception_name`` задан тогда и только тогда, когда
    ``kind is RequestOutcomeKind.RAISED``. Остальные комбинации
    отвергаются в :meth:`__post_init__` — нелегальные состояния
    непредставимы.
    """

    kind: RequestOutcomeKind
    exception_name: str | None = None

    def __post_init__(self) -> None:
        is_raised = self.kind is RequestOutcomeKind.RAISED
        if is_raised and not self.exception_name:
            raise ValueError("RAISED requires exception_name")
        if not is_raised and self.exception_name is not None:
            raise ValueError(f"{self.kind.name} must not carry exception_name")

    @classmethod
    def ok(cls) -> RequestOutcome:
        return cls(RequestOutcomeKind.OK)

    @classmethod
    def cancelled(cls) -> RequestOutcome:
        return cls(RequestOutcomeKind.CANCELLED)

    @classmethod
    def raised(cls, exc: BaseException) -> RequestOutcome:
        return cls(RequestOutcomeKind.RAISED, type(exc).__name__)

    def label(self) -> str:
        """Человеко-читаемая метка: ``ok`` / ``cancelled`` / ``raised:<Exc>``."""
        if self.kind is RequestOutcomeKind.RAISED:
            return f"{self.kind.value}:{self.exception_name}"
        return self.kind.value


class RawLLMObserver(ABC):
    """Наблюдатель сырых данных на границе LLM-адаптера."""

    @abstractmethod
    def on_request(self, kwargs: dict[str, Any]) -> None:
        """Вызывается перед отправкой запроса к LLM с полным набором kwargs."""
        ...

    @abstractmethod
    def on_response_chunk(self, chunk: ChatCompletionChunk) -> None:
        """Вызывается для каждого полученного chunk-а потока ответа."""
        ...

    @abstractmethod
    def on_request_end(self, outcome: RequestOutcome) -> None:
        """Вызывается по завершении потока."""
        ...


class CompositeRawLLMObserver(RawLLMObserver):
    """Fan-out из нескольких :class:`RawLLMObserver` — вызывает каждого по порядку."""

    def __init__(self, observers: Sequence[RawLLMObserver]) -> None:
        self._observers = observers

    def on_request(self, kwargs: dict[str, Any]) -> None:
        for o in self._observers:
            o.on_request(kwargs)

    def on_response_chunk(self, chunk: ChatCompletionChunk) -> None:
        for o in self._observers:
            o.on_response_chunk(chunk)

    def on_request_end(self, outcome: RequestOutcome) -> None:
        for o in self._observers:
            o.on_request_end(outcome)


class FileRawLLMObserver(RawLLMObserver):
    """Пишет сырые запросы/ответы в markdown-файл внутри workspace.

    Каждый вызов — отдельная секция с заголовком (``## Request`` /
    ``## Response chunk``) и блоком ``json``. Файл открывается на каждый
    вызов в режиме append — состояние на уровне файловой системы, между
    перезапусками агента накопление продолжается.
    """

    def __init__(
        self,
        workspace: HistoryWorkspaceShell,
        path: str = "raw_messages.md",
    ) -> None:
        self._workspace = workspace
        self._path = path

    def on_request(self, kwargs: dict[str, Any]) -> None:
        body = json.dumps(kwargs, ensure_ascii=False, indent=2, default=str)
        self._append(f"## Request\n\n```json\n{body}\n```\n\n")

    def on_response_chunk(self, chunk: ChatCompletionChunk) -> None:
        body = chunk.model_dump_json(indent=2)
        self._append(f"## Response chunk\n\n```json\n{body}\n```\n\n")

    def on_request_end(self, outcome: RequestOutcome) -> None:
        self._append(f"## End: {outcome.label()}\n\n")

    def _append(self, text: str) -> None:
        with self._workspace.append_text(self._path) as f:
            f.write(text)


class FileContentObserver(RawLLMObserver):
    """Пишет читаемый markdown-лог запроса/ответа.

    Накапливает потоки ответа по семантическим каналам в переменные,
    в :meth:`on_request_end` собирает их в один markdown-блок и пишет
    в файл одной операцией. На каждый ``on_request`` state сбрасывается;
    запросы аккумулируются в одном файле в режиме append.

    Чтения чанка прямолинейные:

    - ``delta.content`` → ``self._answer``;
    - ``delta.model_extra["reasoning_content"]`` → ``self._reasoning``;
    - ``delta.refusal`` → ``self._refusal``;
    - ``delta.tool_calls[].function.{name,arguments}`` per-index →
      ``self._tool_calls[idx]``;
    - ``choice.finish_reason`` → ``self._finish_reason``;
    - ``chunk.usage`` → ``self._usage``.
    """

    _REASONING_KEY = "reasoning_content"

    def __init__(
        self,
        workspace: HistoryWorkspaceShell,
        path: str = "raw_content.md",
    ) -> None:
        self._workspace = workspace
        self._path = path
        self._reset_state()

    def _reset_state(self) -> None:
        self._request_kwargs: dict[str, Any] = {}
        self._reasoning: list[str] = []
        self._answer: list[str] = []
        self._refusal: list[str] = []
        self._tool_calls: dict[int, dict[str, Any]] = {}
        self._finish_reason: str | None = None
        self._usage: tuple[int, int, int] | None = None

    def on_request(self, kwargs: dict[str, Any]) -> None:
        self._reset_state()
        self._request_kwargs = kwargs

    def on_response_chunk(self, chunk: ChatCompletionChunk) -> None:
        for choice in chunk.choices:
            delta = choice.delta

            r = (delta.model_extra or {}).get(self._REASONING_KEY)
            if r:
                self._reasoning.append(str(r))

            if delta.content:
                self._answer.append(delta.content)

            if delta.refusal:
                self._refusal.append(delta.refusal)

            if delta.tool_calls:
                self._absorb_tool_calls(delta.tool_calls)

            if choice.finish_reason and self._finish_reason is None:
                self._finish_reason = choice.finish_reason

        if chunk.usage is not None and self._usage is None:
            u = chunk.usage
            self._usage = (u.prompt_tokens, u.completion_tokens, u.total_tokens)

    def _absorb_tool_calls(self, tool_calls: Iterable[Any]) -> None:
        for tc in tool_calls:
            entry = self._tool_calls.setdefault(
                tc.index,
                {"name": None, "id": None, "args": []},
            )
            if tc.id:
                entry["id"] = tc.id
            if tc.function:
                if tc.function.name:
                    entry["name"] = tc.function.name
                if tc.function.arguments:
                    entry["args"].append(tc.function.arguments)

    def on_request_end(self, outcome: RequestOutcome) -> None:
        body = json.dumps(
            self._request_kwargs, ensure_ascii=False, indent=2, default=str
        )
        parts: list[str] = [f"\n\n## Request\n\n```json\n{body}\n```\n\n## Response\n"]

        if self._reasoning:
            parts.append("\n### Reasoning\n\n")
            parts.append("".join(self._reasoning))
            parts.append("\n")

        if self._answer:
            parts.append("\n### Answer\n\n")
            parts.append("".join(self._answer))
            parts.append("\n")

        if self._refusal:
            parts.append("\n### Refusal\n\n")
            parts.append("".join(self._refusal))
            parts.append("\n")

        for idx in sorted(self._tool_calls):
            tc = self._tool_calls[idx]
            name = tc["name"] or "(none)"
            tc_id = tc["id"] or "(none)"
            args_text = "".join(tc["args"])
            parts.append(f"\n### Tool call #{idx}: {name} (id={tc_id})\n\n")
            parts.append(f"```\n{args_text}\n```\n")

        if self._finish_reason:
            parts.append(f"\n_finish_reason={self._finish_reason}_\n")

        if self._usage is not None:
            prompt, completion, total = self._usage
            parts.append(
                f"_usage: prompt={prompt} completion={completion} total={total}_\n"
            )

        parts.append(f"\n## End: {outcome.label()}\n")

        with self._workspace.append_text(self._path) as f:
            f.write("".join(parts))


class MetricsRawLLMObserver(RawLLMObserver):
    """Пишет одну строку-сводку в logger по завершении запроса.

    Считает по **сырым** chunk-ам, а не по :class:`AgentEvent` после
    трансформаций — так цифры не искажаются ``StrictJsonContentToolCall`` и
    подобными middleware, которые переупаковывают content в tool-calls.
    Замеряет также elapsed и :class:`RequestOutcome` (с именем
    исключения для ``RAISED``) на wire-слое.
    """

    def __init__(
        self,
        reasoning_extractor: Converter[ChoiceDelta, str | None] | None = None,
    ) -> None:
        self._reasoning_extractor = (
            reasoning_extractor
            if reasoning_extractor is not None
            else MultiKeyReasoningExtractor()
        )
        self._reset()

    def _reset(self) -> None:
        self._start: float | None = None
        self._model: str | None = None
        self._chunks = 0
        self._content_chars = 0
        self._reasoning_chars = 0
        self._tool_arg_chars = 0
        self._tool_call_indices: set[int] = set()
        self._finish_reason: str | None = None

    def on_request(self, kwargs: dict[str, Any]) -> None:
        self._reset()
        self._start = time.monotonic()
        self._model = kwargs.get("model")
        logger.info("LLM request: model=%s", self._model)

    def on_response_chunk(self, chunk: ChatCompletionChunk) -> None:
        self._chunks += 1
        for choice in chunk.choices:
            delta = choice.delta
            if delta.content:
                self._content_chars += len(delta.content)
            reasoning = self._reasoning_extractor.convert(delta)
            if reasoning:
                self._reasoning_chars += len(reasoning)
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    self._tool_call_indices.add(tc.index)
                    if tc.function and tc.function.arguments:
                        self._tool_arg_chars += len(tc.function.arguments)
            if choice.finish_reason:
                self._finish_reason = choice.finish_reason

    def on_request_end(self, outcome: RequestOutcome) -> None:
        elapsed = time.monotonic() - self._start if self._start else 0.0
        logger.info(
            "LLM done: %s, model=%s, chunks=%d, content=%d ch, "
            "reasoning=%d ch, tool_calls=%d (args=%d ch), finish=%s, elapsed=%.2fs",
            outcome.label(),
            self._model,
            self._chunks,
            self._content_chars,
            self._reasoning_chars,
            len(self._tool_call_indices),
            self._tool_arg_chars,
            self._finish_reason,
            elapsed,
        )
