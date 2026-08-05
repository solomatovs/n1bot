"""Вызов web-payload'а: скачивание и поиск идут внутри песочницы."""

from __future__ import annotations

from typing import ClassVar

from pydantic import SecretStr

from boba.toolkit.launcher import ChunkSink, LauncherFactory
from boba.transport.http import HttpProfile
from boba.web.protocol import (
    WebFetchRequest,
    WebFetchTrailer,
    WebGrepRequest,
    WebGrepTrailer,
    WebProfile,
)

__all__ = ["WebCaller"]


class WebCaller:
    """Один вызов payload'а на запрос; профиль соединения едет с ним."""

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.web.payload")

    def __init__(self, tool: str, launchers: LauncherFactory) -> None:
        self._caller = launchers(tool)

    def fetch(  # noqa: PLR0913
        self,
        *,
        url: str,
        profile: HttpProfile,
        as_markdown: bool,
        line_offset: int,
        line_count: int,
        sink: ChunkSink,
    ) -> WebFetchTrailer:
        request = WebFetchRequest(
            op=WebFetchRequest.OP,
            url=url,
            profile=self.transport_of(profile),
            as_markdown=as_markdown,
            line_offset=line_offset,
            line_count=line_count,
        )
        return self._caller.call_stream(self.ENTRY, request, sink, WebFetchTrailer)

    def grep(self, request: WebGrepRequest, sink: ChunkSink) -> WebGrepTrailer:
        return self._caller.call_stream(self.ENTRY, request, sink, WebGrepTrailer)

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
