"""Наблюдатель сырых запросов/ответов LLM на границе OpenAI-клиента.

Позволяет сохранять kwargs ``chat.completions.create`` и каждый входящий
``ChatCompletionChunk`` в неизменённом виде — до любой доменной конверсии.
Используется для отладки, сбора датасетов, анализа поведения моделей через
Ollama/LiteLLM/третьи прокси.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
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
