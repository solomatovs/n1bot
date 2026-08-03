"""Парсинг документа в песочнице: байты -> страницы."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import ClassVar

from boba.tool.doc.liteparse.config import SandboxParserConfig
from boba.tool.doc.liteparse.protocol import (
    ParseBytesAnswer,
    ParseBytesRequest,
)
from boba.toolkit.sandbox import SandboxCaller

__all__ = ["LiteParseCaller"]


class LiteParseCaller:
    """Один вызов payload'а на документ; содержимое едет в запросе."""

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.doc.payload")

    def __init__(
        self,
        tool: str,
        cfg: SandboxParserConfig,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> None:
        self._params = cfg.parse_params()
        self._caller = SandboxCaller(tool, cfg.sandbox.effective(), path_vars)

    def parse_bytes(self, data: bytes, filename: str) -> ParseBytesAnswer:
        request = ParseBytesRequest.of(data, filename, self._params)
        return self._caller.call_json(self.ENTRY, request, ParseBytesAnswer)
