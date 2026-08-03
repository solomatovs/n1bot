"""Вызов web-payload'а: скачивание и поиск идут внутри песочницы."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from pydantic import SecretStr

from boba.chainlit2.agent.tools.web.protocol import (
    WebFetchAnswer,
    WebFetchRequest,
    WebGrepAnswer,
    WebGrepRequest,
    WebProfile,
)
from boba.chainlit2.sandbox import SandboxCaller, SandboxEntryConfig
from boba.transport.http import HttpProfile

__all__ = ["WebCaller"]


class WebCaller:
    """Один вызов payload'а на запрос; профиль соединения едет с ним."""

    def __init__(
        self,
        tool: str,
        sandbox: SandboxEntryConfig,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> None:
        self._entry = sandbox.entry
        self._caller = SandboxCaller(tool, sandbox.effective(), path_vars)

    def fetch(
        self,
        *,
        url: str,
        profile: HttpProfile,
        as_markdown: bool,
        line_offset: int,
        line_count: int,
    ) -> WebFetchAnswer:
        request = WebFetchRequest(
            op=WebFetchRequest.OP,
            url=url,
            profile=self.transport_of(profile),
            as_markdown=as_markdown,
            line_offset=line_offset,
            line_count=line_count,
        )
        return self._caller.call_json(self._entry, request, WebFetchAnswer)

    def grep(self, request: WebGrepRequest) -> WebGrepAnswer:
        return self._caller.call_json(self._entry, request, WebGrepAnswer)

    @staticmethod
    def transport_of(profile: HttpProfile) -> WebProfile:
        """Креды раскрываются здесь: SecretStr не сериализуется сам собой."""
        auth: dict[str, str] = {}
        for name, value in profile.auth.model_dump().items():
            if isinstance(value, SecretStr):
                auth[name] = value.get_secret_value()
                continue
            auth[name] = str(value)
        return WebProfile(
            timeout_sec=profile.timeout_sec,
            ssl_verify=profile.ssl_verify,
            auth=auth,
        )
