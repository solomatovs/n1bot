"""JsonLines реализация MessageService — диалог в файле workspace."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from boba.agent.messages import (
    MessageService,
    MessageStoreReadError,
    MessageStoreWriteError,
)
from boba.llm.models import (
    AssistantMessage,
    InvalidToolCall,
    Message,
    MessageId,
    SystemMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from boba.workspace.contract import HistoryWorkspaceShell, WorkspaceError


class JsonLinesMessageService(MessageService):
    """Journaling-реализация MessageService поверх workspace-файла."""

    _DEFAULT_FILENAME = "messages.jsonl"

    def __init__(
        self,
        workspace: HistoryWorkspaceShell,
        filename: str = _DEFAULT_FILENAME,
    ) -> None:
        self._workspace = workspace
        self._filename = filename
        self._messages: list[Message] = []
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

    def add(self, message: Message) -> None:
        line = self._encode(message)
        try:
            with self._workspace.append_text(self._filename) as f:
                f.write(line)
                f.write("\n")
        except WorkspaceError as exc:
            raise MessageStoreWriteError(exc, ctx=f"path={self._filename}") from exc
        self._messages.append(message)

    def message_iter(self) -> Iterator[Message]:
        return iter(self._messages)

    def last(self) -> Message | None:
        return self._messages[-1] if self._messages else None

    def clear(self) -> None:
        try:
            with self._workspace.write_text(self._filename):
                pass
        except WorkspaceError as exc:
            raise MessageStoreWriteError(exc, ctx=f"path={self._filename}") from exc
        self._messages.clear()

    @staticmethod
    def _encode(message: Message) -> str:
        match message:
            case SystemMessage(id=mid, content=content):
                payload: dict[str, Any] = {
                    "type": "system",
                    "id": mid.to_wire(),
                    "content": content,
                }
            case UserMessage(id=mid, content=content):
                payload = {
                    "type": "user",
                    "id": mid.to_wire(),
                    "content": content,
                }
            case AssistantMessage(
                id=mid,
                content=content,
                tool_calls=tcs,
                invalid_tool_calls=itcs,
            ):
                payload = {
                    "type": "assistant",
                    "id": mid.to_wire(),
                    "content": content,
                    "tool_calls": [
                        {"id": tc.id, "name": tc.name, "args": dict(tc.args)}
                        for tc in tcs
                    ],
                    "invalid_tool_calls": [
                        {
                            "id": itc.id,
                            "name": itc.name,
                            "raw_args": itc.raw_args,
                            "error": itc.error,
                        }
                        for itc in itcs
                    ],
                }
            case ToolResultMessage(
                id=mid, content=content, tool_call_id=tcid, success=success,
            ):
                payload = {
                    "type": "tool_result",
                    "id": mid.to_wire(),
                    "tool_call_id": tcid,
                    "content": content,
                    "success": success,
                }
            case _:
                msg = f"JsonLinesMessageService: неизвестный тип Message: {type(message).__name__}"
                raise ValueError(msg)
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _decode(line: str) -> Message:
        raw = json.loads(line)
        msg_type = raw["type"]
        mid = MessageId.from_wire(raw["id"])
        match msg_type:
            case "system":
                return SystemMessage(id=mid, content=raw["content"])
            case "user":
                return UserMessage(id=mid, content=raw["content"])
            case "assistant":
                tool_calls = tuple(
                    ToolCall(id=tc["id"], name=tc["name"], args=tc["args"])
                    for tc in raw.get("tool_calls", [])
                )
                invalid_tool_calls = tuple(
                    InvalidToolCall(
                        id=itc["id"],
                        name=itc["name"],
                        raw_args=itc["raw_args"],
                        error=itc["error"],
                    )
                    for itc in raw.get("invalid_tool_calls", [])
                )
                return AssistantMessage(
                    id=mid,
                    content=raw.get("content", ""),
                    tool_calls=tool_calls,
                    invalid_tool_calls=invalid_tool_calls,
                )
            case "tool_result":
                return ToolResultMessage(
                    id=mid,
                    tool_call_id=raw["tool_call_id"],
                    content=raw["content"],
                    success=raw.get("success", True),
                )
            case _:
                msg = f"JsonLinesMessageService: неизвестный type='{msg_type}'"
                raise ValueError(msg)
