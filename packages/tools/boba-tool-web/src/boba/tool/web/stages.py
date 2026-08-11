"""Обогатители web-узлов: args модели -> запрос payload'а.

Профиль соединения выбирается whitelist'ом хостов, лимиты выдачи приходят из
конфига инструмента; в args узла и в спеке графа их нет. Креды профиля остаются
SecretStr и раскрываются сериализацией запроса в tool_args.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import JsonValue

from boba.tool.web._grep import WebGrepConfig
from boba.toolkit.workflow import RequestArgs, StageNode
from boba.web.protocol import (
    WebFetchArgs,
    WebFetchRequest,
    WebGrepArgs,
    WebGrepRequest,
    WebNodes,
    WebOp,
    WebProfile,
)

__all__ = ["WebStages"]


class WebStages:
    """Обогатители узлов web_fetch и web_grep на конфиге инструмента."""

    def __init__(self, cfg: WebGrepConfig) -> None:
        self._cfg = cfg

    def fetch(self, args: Mapping[str, JsonValue], /) -> Mapping[str, Any]:
        call = WebFetchArgs.model_validate(args)

        profile = WebProfile.of(self._cfg.resolve_profile(call.url))

        request = WebFetchRequest(
            op=WebOp.FETCH,
            url=call.url,
            as_markdown=call.as_markdown,
            line_offset=call.line_offset,
            line_count=call.line_count,
            profile=profile,
        )

        return RequestArgs.of(request)

    def grep(self, args: Mapping[str, JsonValue], /) -> Mapping[str, Any]:
        call = WebGrepArgs.model_validate(args)

        profile = WebProfile.of(self._cfg.resolve_profile(call.url))

        request = WebGrepRequest(
            op=WebOp.GREP,
            url=call.url,
            pattern=call.pattern,
            as_markdown=call.as_markdown,
            case_insensitive=call.case_insensitive,
            context=call.context,
            limit=call.limit,
            fixed_string=call.fixed_string,
            profile=profile,
            max_text_chars=self._cfg.max_text_chars,
        )

        return RequestArgs.of(request)

    def stages(self) -> dict[str, StageNode]:
        """Узлы пакета для реестра стадий; профиль подставляет приложение."""
        nodes: dict[str, StageNode] = {}

        nodes[WebOp.FETCH] = StageNode(
            contract=WebNodes.FETCH,
            entry=WebNodes.ENTRY,
            request=WebFetchRequest,
            enrich=self.fetch,
        )

        nodes[WebOp.GREP] = StageNode(
            contract=WebNodes.GREP,
            entry=WebNodes.ENTRY,
            request=WebGrepRequest,
            enrich=self.grep,
        )

        return nodes
