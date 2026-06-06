"""WebUrlsRequestSource: список URL'ов → `HttpRequest` поток.

Single source: URL'ы проверяются против `WebConnection.profiles`-whitelist'а
**до** HTTP — запрещённый host бросает `ValueError` ещё на стадии stream'а
(никаких сетевых попыток). Auth для запроса берётся из профиля хоста
(`resolve_profile(url).auth.httpx_auth()`).

`source_id = SourceId(url)` — каноничный id документа (тот же URL,
который LLM просил). Дальше по pipeline'у этот id пробрасывается без
изменений в `RawDocument`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from boba.indexing import Metadata, PipelineContext, RequestSource, SourceId
from boba.tool.web.connection import WebConnection
from boba.transport.http import HttpRequest

__all__ = ["WebUrlsRequestSource"]


class WebUrlsRequestSource(RequestSource[HttpRequest]):
    """Явный список URL'ов; auth берётся из профиля хоста (`connection.profiles`)."""

    def __init__(
        self,
        *,
        urls: Sequence[str],
        connection: WebConnection,
    ) -> None:
        self._urls = tuple(urls)
        self._connection = connection

    def name(self) -> str:
        return f"WebUrlsRequestSource({len(self._urls)} urls)"

    def stream(self, ctx: PipelineContext) -> Iterable[HttpRequest]:
        del ctx
        for url in self._urls:
            profile = self._connection.resolve_profile(url)
            yield HttpRequest(
                url=url,
                source_id=SourceId(url),
                method="GET",
                headers={},
                auth=profile.auth.httpx_auth(),
                metadata=Metadata.empty(),
            )
