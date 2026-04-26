"""JsonLines реализация :class:`MessageService` — диалог в файле workspace.

Персистит каждое сообщение одной строкой JSON. При создании инстанса
восстанавливает диалог из файла в память, далее работает как in-memory
список + append к файлу на каждое :meth:`add`. Замена схемы
``HistoryService + HistoryReplayMiddleware + HistoryPersistMiddleware``
одной реализацией сервиса сообщений: знание о межзапускной персистенции
живёт в конкретной имплементации, а не в пайплайне middleware.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict

from boba.domain.agent.messages import (
    MessageService,
    MessageStoreReadError,
    MessageStoreWriteError,
)
from boba.domain.core.workspace import HistoryWorkspaceShell, WorkspaceError
from boba.domain.llm.models import LLMMessage, LLMToolCall


class JsonLinesMessageService(MessageService):
    """Journaling-реализация :class:`MessageService` поверх workspace-файла."""

    _DEFAULT_FILENAME = "messages.jsonl"

    def __init__(
        self,
        workspace: HistoryWorkspaceShell,
        filename: str = _DEFAULT_FILENAME,
    ) -> None:
        self._workspace = workspace
        self._filename = filename
        self._messages: list[LLMMessage] = []
        self._ensure_file()
        self._load()

    def _ensure_file(self) -> None:
        try:
            if self._workspace.exists(self._filename):
                return
            with self._workspace.write_text(self._filename):
                pass
        except WorkspaceError as exc:
            raise MessageStoreWriteError(exc, ctx=f"path={self._filename}") from exc

    def _load(self) -> None:
        try:
            for line in self._workspace.read_lines(self._filename):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    self._messages.append(self._decode(stripped))
                except (json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
                    raise MessageStoreReadError(
                        exc, ctx=f"path={self._filename}: {stripped!r}"
                    ) from exc
        except WorkspaceError as exc:
            raise MessageStoreReadError(exc, ctx=f"path={self._filename}") from exc

    def add(self, message: LLMMessage) -> None:
        line = self._encode(message)
        try:
            with self._workspace.append_text(self._filename) as f:
                f.write(line)
                f.write("\n")
        except WorkspaceError as exc:
            raise MessageStoreWriteError(exc, ctx=f"path={self._filename}") from exc
        self._messages.append(message)

    def message_iter(self) -> Iterator[LLMMessage]:
        return iter(self._messages)

    def last(self) -> LLMMessage | None:
        return self._messages[-1] if self._messages else None

    def clear(self) -> None:
        try:
            with self._workspace.write_text(self._filename):
                pass
        except WorkspaceError as exc:
            raise MessageStoreWriteError(exc, ctx=f"path={self._filename}") from exc
        self._messages.clear()

    @staticmethod
    def _encode(message: LLMMessage) -> str:
        return json.dumps(asdict(message), ensure_ascii=False)

    @staticmethod
    def _decode(line: str) -> LLMMessage:
        raw = json.loads(line)
        tool_calls = tuple(LLMToolCall(**tc) for tc in raw.get("tool_calls", []))
        return LLMMessage(
            role=raw["role"],
            content=raw["content"],
            tool_call_id=raw.get("tool_call_id"),
            tool_calls=tool_calls,
        )
