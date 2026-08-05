"""Точка входа payload'а: запрос со stdin, кадры и трейлер в stdout."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Coroutine
from typing import Any, ClassVar, TypeAlias

from boba.toolkit.launcher import LaunchPayload

__all__ = ["ChunkEmitter", "PayloadEntry"]

ChunkEmitter: TypeAlias = Callable[[str], None]

Dispatch: TypeAlias = Callable[
    [dict[str, Any], ChunkEmitter],
    Coroutine[Any, Any, dict[str, Any]],
]


class PayloadEntry:
    """Разбор запроса, печать кадров и трейлера; операцию выбирает инструмент."""

    CHUNK_CHARS: ClassVar[int] = 64 * 1024

    @staticmethod
    def emit_text(emit: ChunkEmitter, text: str) -> None:
        """Материализованный текст уходит кадрами ограниченного размера."""
        for start in range(0, len(text), PayloadEntry.CHUNK_CHARS):
            emit(text[start : start + PayloadEntry.CHUNK_CHARS])

    @staticmethod
    def main(dispatch: Dispatch) -> int:
        request = json.loads(sys.stdin.read())
        trailer = asyncio.run(dispatch(request, PayloadEntry.emit))
        PayloadEntry._write_trailer(trailer)
        return 0

    @staticmethod
    def emit(chunk: str) -> None:
        """Кадр уходит сразу: flush отдаёт данные хосту, не дожидаясь конца."""
        sys.stdout.write(LaunchPayload.encode_chunk(chunk))
        sys.stdout.write("\n")
        sys.stdout.flush()

    @staticmethod
    def _write_trailer(trailer: dict[str, Any]) -> None:
        body = json.dumps(trailer, ensure_ascii=False)
        sys.stdout.write(LaunchPayload.MARKER)
        sys.stdout.write(body)
        sys.stdout.write("\n")
        sys.stdout.flush()
