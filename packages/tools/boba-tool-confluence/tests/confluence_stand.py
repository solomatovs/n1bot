"""Стенд Confluence для ingest-тестов: живой REST на uvicorn с состоянием.

Заглушка держит страницы и вложения в памяти и отвечает теми же формами, что
Confluence Server: content/search с expand версий и вложений, тело страницы
content/{id}, пагинированный child/attachment, download вложений, проба
`id in (...)`. Тест меняет состояние напрямую (правка, перезаливка, удаление)
и считает запросы по счётчикам — так видно, что неизменившееся не качается.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from types import TracebackType
from typing import Any, ClassVar, Self, TypeVar

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

__all__ = [
    "ConfluenceStub",
    "LiveServer",
    "StubAttachment",
    "StubPage",
    "StubRoute",
    "Window",
]

T = TypeVar("T")


class StubRoute(StrEnum):
    """Какие запросы считает заглушка."""

    SEARCH = "search"
    SPACE = "space"
    SPACE_CONTENT = "space_content"
    BODY = "body"
    ATTACHMENTS = "attachments"
    DOWNLOAD = "download"


@dataclass
class StubAttachment:
    """Вложение страницы; версия растёт при каждой перезаливке."""

    id: str
    title: str
    media_type: str
    content: bytes
    version: int = 1
    when: str = "2026-01-01T00:00:00.000Z"
    broken: bool = False

    def upload(self, content: bytes, *, when: str) -> None:
        self.content = content
        self.version += 1
        self.when = when

    def json(self, page_id: str) -> dict[str, Any]:
        download = httpx.URL(
            path=f"/download/attachments/{page_id}/{self.title}",
            params={"version": self.version, "api": "v2"},
        )
        return {
            "id": self.id,
            "title": self.title,
            "version": {"number": self.version, "when": self.when},
            "extensions": {"mediaType": self.media_type, "fileSize": len(self.content)},
            "_links": {
                "download": str(download),
                "webui": f"/pages/viewpageattachments.action?pageId={page_id}",
            },
        }


@dataclass
class StubPage:
    """Страница спейса; версия растёт при правке тела или заголовка."""

    id: str
    space: str
    title: str
    html: str
    version: int = 1
    when: str = "2026-01-01T00:00:00.000Z"
    attachments: list[StubAttachment] = field(default_factory=list)
    broken: bool = False
    missing: bool = False
    """Страница пропала: список её ещё отдаёт, а тело отвечает 404."""

    def edit(self, *, html: str | None = None, title: str | None = None) -> None:
        if html is not None:
            self.html = html

        if title is not None:
            self.title = title

        self.version += 1

    def attachment(self, title: str) -> StubAttachment:
        for att in self.attachments:
            if att.title == title:
                return att

        msg = f"stub page {self.id}: no attachment {title!r}"
        raise KeyError(msg)

    def summary(self, *, expansion_limit: int) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for att in self.attachments[:expansion_limit]:
            results.append(att.json(self.id))

        return {
            "id": self.id,
            "type": "page",
            "title": self.title,
            "space": {"key": self.space},
            "version": {"number": self.version, "when": self.when},
            "ancestors": [{"title": "Home"}],
            "children": {
                "attachment": {
                    "results": results,
                    "size": len(results),
                    "limit": expansion_limit,
                }
            },
            "_links": {"webui": f"/pages/{self.id}"},
        }

    def body(self) -> dict[str, Any]:
        data = self.summary(expansion_limit=0)
        data["body"] = {"view": {"value": self.html}}
        return data


@dataclass(frozen=True)
class Window:
    """Окно листинга: start/limit запроса и вырезка по ним."""

    start: int
    limit: int

    def of(self, items: list[T]) -> list[T]:
        return items[self.start : self.start + self.limit]

    def has_more(self, total: int) -> bool:
        return self.start + self.limit < total

    def next_start(self) -> int:
        return self.start + self.limit


class ConfluenceStub:
    """Состояние и маршруты заглушки; один экземпляр на тест."""

    EXPANSION_LIMIT: ClassVar[int] = 25
    """Столько вложений Confluence раскрывает в списке; остальное — child/attachment."""

    LISTING_LIMIT: ClassVar[int] = 10
    """Размер страницы child/attachment: меньше, чтобы пагинация точно сработала."""

    PAGE_LIMIT: ClassVar[int] = 25
    """limit по умолчанию у листингов страниц, как у Confluence Server."""

    ATTACHMENT_LIMIT: ClassVar[int] = 50
    """limit по умолчанию у child/attachment; сверху его режет LISTING_LIMIT."""

    SPACE_RE: ClassVar[re.Pattern[str]] = re.compile(r'space\s*=\s*"([^"]+)"')
    ID_RE: ClassVar[re.Pattern[str]] = re.compile(r'id\s*=\s*"([^"]+)"')
    IDS_RE: ClassVar[re.Pattern[str]] = re.compile(r"id\s+in\s*\(([^)]*)\)")

    def __init__(self) -> None:
        self.pages: dict[str, StubPage] = {}
        self.calls: Counter[StubRoute] = Counter()
        self.spaces: set[str] = set()
        self.archived: set[str] = set()
        """Архивные спейсы: поиск их контент не отдаёт, список спейса — отдаёт."""

    def has_space(self, key: str) -> bool:
        """Спейс есть, если объявлен явно или в нём есть хоть одна страница."""
        keys = set(self.spaces)
        for page in self.pages.values():
            keys.add(page.space)

        return key in keys

    def archive(self, key: str) -> None:
        """Спейс уходит в архив: как в Confluence, из поиска он пропадает."""
        self.spaces.add(key)
        self.archived.add(key)

    def status_of(self, key: str) -> str:
        if key in self.archived:
            return "archived"

        return "current"

    def pages_of(self, key: str) -> list[StubPage]:
        """Страницы спейса в порядке id — так их отдаёт список контента."""
        found: list[StubPage] = []
        for page in self.pages.values():
            if page.space != key:
                continue

            found.append(page)

        found.sort(key=lambda page: page.id)
        return found

    def add(self, page: StubPage) -> StubPage:
        self.pages[page.id] = page
        return page

    def delete(self, page_id: str) -> None:
        del self.pages[page_id]

    def reset_calls(self) -> None:
        self.calls.clear()

    def app(self) -> FastAPI:
        app = FastAPI()
        self._route_search(app)
        self._route_space(app)
        self._route_space_content(app)
        self._route_attachments(app)
        self._route_body(app)
        self._route_download(app)
        return app

    def _route_search(self, app: FastAPI) -> None:
        @app.get("/rest/api/content/search")
        async def search(request: Request) -> Response:
            self.calls[StubRoute.SEARCH] += 1
            matched = self._match(request.query_params.get("cql", ""))
            window = self._window(request, default=self.PAGE_LIMIT)
            results: list[dict[str, Any]] = []
            for page in window.of(matched):
                results.append(page.summary(expansion_limit=self.EXPANSION_LIMIT))

            data = self._listing(
                results=results,
                window=window,
                total=len(matched),
                path="/rest/api/content/search",
                request=request,
            )
            data["totalSize"] = len(matched)
            return self._json(data)

    def _route_space(self, app: FastAPI) -> None:
        @app.get("/rest/api/space/{key}")
        async def space(key: str) -> Response:
            self.calls[StubRoute.SPACE] += 1
            if not self.has_space(key):
                return Response(status_code=404)

            return self._json(
                {
                    "key": key,
                    "name": key,
                    "type": "global",
                    "status": self.status_of(key),
                }
            )

    def _route_space_content(self, app: FastAPI) -> None:
        @app.get("/rest/api/space/{key}/content/page")
        async def space_content(key: str, request: Request) -> Response:
            self.calls[StubRoute.SPACE_CONTENT] += 1
            if not self.has_space(key):
                return Response(status_code=404)

            pages = self.pages_of(key)
            window = self._window(request, default=self.PAGE_LIMIT)
            results: list[dict[str, Any]] = []
            for page in window.of(pages):
                results.append(page.summary(expansion_limit=self.EXPANSION_LIMIT))

            data = self._listing(
                results=results,
                window=window,
                total=len(pages),
                path=f"/rest/api/space/{key}/content/page",
                request=request,
            )
            return self._json(data)

    def _route_attachments(self, app: FastAPI) -> None:
        @app.get("/rest/api/content/{page_id}/child/attachment")
        async def attachments(page_id: str, request: Request) -> Response:
            self.calls[StubRoute.ATTACHMENTS] += 1
            page = self.pages.get(page_id)
            if page is None:
                return Response(status_code=404)

            window = self._window(
                request,
                default=self.ATTACHMENT_LIMIT,
                cap=self.LISTING_LIMIT,
            )
            results: list[dict[str, Any]] = []
            for att in window.of(page.attachments):
                results.append(att.json(page_id))

            data = self._listing(
                results=results,
                window=window,
                total=len(page.attachments),
                path=f"/rest/api/content/{page_id}/child/attachment",
                request=request,
            )
            return self._json(data)

    def _route_body(self, app: FastAPI) -> None:
        @app.get("/rest/api/content/{page_id}")
        async def body(page_id: str) -> Response:
            self.calls[StubRoute.BODY] += 1
            page = self.pages.get(page_id)
            if page is None or page.missing:
                return Response(status_code=404)

            if page.broken:
                return Response(status_code=500)

            return self._json(page.body())

    def _route_download(self, app: FastAPI) -> None:
        @app.get("/download/attachments/{page_id}/{title}")
        async def download(page_id: str, title: str) -> Response:
            self.calls[StubRoute.DOWNLOAD] += 1
            page = self.pages.get(page_id)
            if page is None:
                return Response(status_code=404)

            att = page.attachment(title)
            if att.broken:
                return Response(status_code=500)

            return Response(content=att.content, media_type=att.media_type)

    @staticmethod
    def _window(request: Request, *, default: int, cap: int = 0) -> Window:
        """Окно из запроса; cap — потолок limit'а, как у Confluence."""
        start = int(request.query_params.get("start", "0"))
        limit = int(request.query_params.get("limit", str(default)))
        if cap:
            limit = min(limit, cap)

        return Window(start=start, limit=limit)

    @staticmethod
    def _listing(
        *,
        results: list[dict[str, Any]],
        window: Window,
        total: int,
        path: str,
        request: Request,
    ) -> dict[str, Any]:
        """Страница выдачи со ссылкой next — форма, общая у всех листингов."""
        data: dict[str, Any] = {
            "results": results,
            "start": window.start,
            "limit": window.limit,
            "size": len(results),
            "_links": {},
        }
        if not window.has_more(total):
            return data

        params = dict(request.query_params)
        params["start"] = str(window.next_start())
        data["_links"]["next"] = str(httpx.URL(path=path, params=params))
        return data

    def _match(self, cql: str) -> list[StubPage]:
        if match := self.IDS_RE.search(cql):
            wanted = set(re.findall(r'"([^"]+)"', match.group(1)))
            return self._ordered(lambda page: page.id in wanted)

        if match := self.ID_RE.search(cql):
            page_id = match.group(1)
            return self._ordered(lambda page: page.id == page_id)

        if match := self.SPACE_RE.search(cql):
            space = match.group(1)
            return self._ordered(lambda page: page.space == space)

        msg = f"stub confluence: unsupported cql {cql!r}"
        raise ValueError(msg)

    def _ordered(self, keep: Any) -> list[StubPage]:
        """Совпавшие страницы поиска; контент архивных спейсов в индекс не попадает."""
        pages: list[StubPage] = []
        for page_id in sorted(self.pages):
            page = self.pages[page_id]
            if page.space in self.archived:
                continue

            if keep(page):
                pages.append(page)

        return pages

    @staticmethod
    def _json(data: dict[str, Any]) -> Response:
        return Response(content=json.dumps(data), media_type="application/json")


class LiveServer:
    """uvicorn на свободном порту в текущем цикле событий."""

    STARTUP_POLL_SEC: ClassVar[float] = 0.02

    def __init__(self, app: FastAPI) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        self._server = uvicorn.Server(config)
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        self._task = asyncio.create_task(self._server.serve())

        while not self._server.started:
            if self._task.done():
                self._task.result()
                msg = (
                    "fake confluence uvicorn on 127.0.0.1: serve() returned "
                    "before the server reported started"
                )
                raise RuntimeError(msg)

            await asyncio.sleep(self.STARTUP_POLL_SEC)

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._server.should_exit = True
        if self._task is not None:
            await self._task

    @property
    def port(self) -> int:
        return self._server.servers[0].sockets[0].getsockname()[1]
