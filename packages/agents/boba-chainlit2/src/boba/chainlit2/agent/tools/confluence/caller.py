"""Вызовы confluence-payload'а: сеть и разбор идут внутри песочницы."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from boba.chainlit2.agent.tools.confluence.protocol import (
    ConfluenceAttachmentAnswer,
    ConfluenceAttachmentRequest,
    ConfluenceGrepAnswer,
    ConfluenceGrepRequest,
    ConfluencePageAnswer,
    ConfluencePageRequest,
    ConfluenceSearchAnswer,
    ConfluenceSearchRequest,
    ConfluenceSpacesAnswer,
    ConfluenceSpacesRequest,
)
from boba.chainlit2.agent.tools.web.caller import WebCaller
from boba.chainlit2.sandbox import SandboxCaller, SandboxEntryConfig
from boba.transport.http import HttpProfile

__all__ = ["ConfluenceCaller"]


class ConfluenceCaller:
    """Один вызов payload'а на операцию; профиль соединения едет с ним."""

    def __init__(
        self,
        tool: str,
        sandbox: SandboxEntryConfig,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> None:
        self._entry = sandbox.entry
        self._caller = SandboxCaller(tool, sandbox.effective(), path_vars)

    @staticmethod
    def transport_of(profile: HttpProfile):
        return WebCaller.transport_of(profile)

    def page(self, request: ConfluencePageRequest) -> ConfluencePageAnswer:
        return self._caller.call_json(self._entry, request, ConfluencePageAnswer)

    def grep(self, request: ConfluenceGrepRequest) -> ConfluenceGrepAnswer:
        return self._caller.call_json(self._entry, request, ConfluenceGrepAnswer)

    def search(self, request: ConfluenceSearchRequest) -> ConfluenceSearchAnswer:
        return self._caller.call_json(self._entry, request, ConfluenceSearchAnswer)

    def spaces(self, request: ConfluenceSpacesRequest) -> ConfluenceSpacesAnswer:
        return self._caller.call_json(self._entry, request, ConfluenceSpacesAnswer)

    def attachment(
        self, request: ConfluenceAttachmentRequest
    ) -> ConfluenceAttachmentAnswer:
        return self._caller.call_json(
            self._entry, request, ConfluenceAttachmentAnswer
        )
