"""ConfluenceSpaceRequestSource: все страницы одного space.

Multi-space форма (`ConfluenceMultiSpaceRequestSource`) — composite над
несколькими single-space источниками; для multi-space ingest без
дублирования pipeline-кода.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from boba.indexing import PipelineContext, RequestSource
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.request_sources._common import (
    confluence_discover_space_pages,
    extract_host,
    make_page_request,
)
from boba.transport.http import HttpRequest

__all__ = [
    "ConfluenceMultiSpaceRequestSource",
    "ConfluenceSpaceRequestSource",
]


class ConfluenceSpaceRequestSource(RequestSource[HttpRequest]):
    """Все страницы space через `/rest/api/space/{key}/content`."""

    def __init__(
        self,
        *,
        conn: ConfluenceConnection,
        space_key: str,
        body_format: str = "export_view",
    ) -> None:
        self._conn = conn
        self._space_key = space_key
        self._body_format = body_format
        self._host = extract_host(conn.base_url)

    def name(self) -> str:
        return f"ConfluenceSpaceRequestSource({self._host}/{self._space_key})"

    def stream(self, ctx: PipelineContext) -> Iterable[HttpRequest]:
        del ctx
        auth = self._conn.make_auth()
        for page_id in confluence_discover_space_pages(
            self._conn, self._space_key,
        ):
            yield make_page_request(
                base_url=self._conn.base_url,
                host=self._host,
                auth=auth,
                page_id=page_id,
                body_format=self._body_format,
            )


class ConfluenceMultiSpaceRequestSource(RequestSource[HttpRequest]):
    """Все страницы из НЕСКОЛЬКИХ space'ов — последовательно через
    `ConfluenceSpaceRequestSource` для каждого ключа.

    Pipeline-семантика: всё ведёт себя как ОДНА выгрузка над union страниц.
    Cleanup идёт через touch-based mark (reconcile refresh'ит updated_at для
    всех виденных chunk'ов; `FullCleanup` сносит остальные).
    """

    def __init__(
        self,
        *,
        conn: ConfluenceConnection,
        space_keys: Sequence[str],
        body_format: str = "export_view",
    ) -> None:
        if not space_keys:
            raise ValueError("space_keys пуст")
        self._inner = [
            ConfluenceSpaceRequestSource(
                conn=conn,
                space_key=k,
                body_format=body_format,
            )
            for k in space_keys
        ]
        self._space_keys = tuple(space_keys)
        self._host = extract_host(conn.base_url)

    def name(self) -> str:
        keys = ",".join(self._space_keys)
        return f"ConfluenceMultiSpaceRequestSource({self._host}/[{keys}])"

    def stream(self, ctx: PipelineContext) -> Iterable[HttpRequest]:
        for src in self._inner:
            yield from src.stream(ctx)
