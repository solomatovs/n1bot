"""Точка входа payload'а: запрос со stdin, ответ маркерной строкой в stdout."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

from boba.toolkit.sandbox.payload import SandboxPayload

__all__ = ["PayloadEntry"]


class PayloadEntry:
    """Разбор запроса и печать ответа; операцию выбирает сам инструмент."""

    @staticmethod
    def main(dispatch: Callable[[dict[str, Any]], dict[str, Any]]) -> int:
        request = json.loads(sys.stdin.read())
        answer = dispatch(request)
        body = json.dumps(answer, ensure_ascii=False)
        sys.stdout.write(f"{SandboxPayload.MARKER}{body}\n")
        return 0
