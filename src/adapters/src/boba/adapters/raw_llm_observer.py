"""Наблюдатель сырых запросов/ответов LLM на границе OpenAI-клиента.

Позволяет сохранять kwargs ``chat.completions.create`` и каждый входящий
``ChatCompletionChunk`` в неизменённом виде — до любой доменной конверсии.
Используется для отладки, сбора датасетов, анализа поведения моделей через
Ollama/LiteLLM/третьи прокси.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any

from openai.types.chat.chat_completion_chunk import ChatCompletionChunk

from boba.domain.core.workspace import WorkspaceService


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


class CompositeRawLLMObserver(RawLLMObserver):
    """Fan-out из нескольких :class:`RawLLMObserver` — вызывает каждого по порядку."""

    def __init__(self, observers: Iterable[RawLLMObserver]) -> None:
        self._observers = list(observers)

    def on_request(self, kwargs: dict[str, Any]) -> None:
        for o in self._observers:
            o.on_request(kwargs)

    def on_response_chunk(self, chunk: ChatCompletionChunk) -> None:
        for o in self._observers:
            o.on_response_chunk(chunk)


class FileRawLLMObserver(RawLLMObserver):
    """Пишет сырые запросы/ответы в markdown-файл внутри workspace.

    Каждый вызов — отдельная секция с заголовком (``## Request`` /
    ``## Response chunk``) и блоком ``json``. Файл открывается на каждый
    вызов в режиме append — состояние на уровне файловой системы, между
    перезапусками агента накопление продолжается.
    """

    def __init__(
        self,
        workspace: WorkspaceService,
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

    def _append(self, text: str) -> None:
        with self._workspace.append_text(self._path) as f:
            f.write(text)


class FileContentObserver(RawLLMObserver):
    """Пишет читаемый лог: заголовок Request с JSON kwargs, заголовок
    Response — и дальше склеенный текст из поля ``delta.content`` каждого
    chunk-а как есть.

    В отличие от :class:`FileRawLLMObserver`, не разделяет ответ на chunk-и
    и не оборачивает в JSON — читается как обычный текст, который модель
    сгенерировала. Удобно для быстрого просмотра/дифф-анализа поведения.
    """

    def __init__(
        self,
        workspace: WorkspaceService,
        path: str = "raw_content.md",
    ) -> None:
        self._workspace = workspace
        self._path = path

    def on_request(self, kwargs: dict[str, Any]) -> None:
        body = json.dumps(kwargs, ensure_ascii=False, indent=2, default=str)
        self._append(
            f"\n\n## Request\n\n```json\n{body}\n```\n\n## Response\n\n"
        )

    def on_response_chunk(self, chunk: ChatCompletionChunk) -> None:
        for choice in chunk.choices:
            content = choice.delta.content
            if content:
                self._append(content)

    def _append(self, text: str) -> None:
        with self._workspace.append_text(self._path) as f:
            f.write(text)
