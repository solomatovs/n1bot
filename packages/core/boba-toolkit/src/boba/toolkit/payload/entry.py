"""Точка входа payload'а: запрос со stdin, ответ маркерной строкой в stdout."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Coroutine
from typing import Any

from boba.toolkit.sandbox.payload import SandboxPayload

__all__ = ["PayloadEntry"]


class PayloadEntry:
    """Разбор запроса и печать ответа; операцию выбирает сам инструмент."""

    @staticmethod
    def main(dispatch: Callable[[dict[str, Any]], dict[str, Any]]) -> int:
        request = json.loads(sys.stdin.read())
        answer = dispatch(request)
        PayloadEntry._write(answer)
        return 0

    @staticmethod
    def main_async(
        dispatch: Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]],
    ) -> int:
        """Вход для операций с postgres: он в системе существует только как async."""
        request = json.loads(sys.stdin.read())
        answer = asyncio.run(dispatch(request))
        PayloadEntry._write(answer)
        return 0

    @staticmethod
    def _write(answer: dict[str, Any]) -> None:
        body = json.dumps(answer, ensure_ascii=False)
        sys.stdout.write(f"{SandboxPayload.MARKER}{body}\n")
