"""Стенд поисковой выдачи: страница с полем запроса, три колонки результатов (fts,
trgm, vector) и подсказки при наборе (suggest) поверх схемы ix. Один процесс на
стандартном http.server: отдаёт index.html, /surfaces и /search.

Сам поиск живёт в ядре (boba.ix_core.search): стенд при старте читает реестры схемы
в SearchRegistry, а на запрос берёт соединение из пула, считает вектор своей
моделью для режима vector и отдаёт SearchRequest в IxSearch. Поэтому новая таблица
индекса появляется в поиске после перезапуска стенда, а правка ранжирования в
sql-файлах ядра видна без перезапуска.

Поверхности выдачи выбирает человек на странице: список стенд берёт из словаря,
оставляя те, у которых объявлены аспекты. Выбранное уходит в запрос списком; не
выбрано ничего — ищем везде.

Пул к ix и эмбеддер живут в event loop главного потока; http-сервер отвечает из
своих потоков и отдаёт корутину поиска в этот loop.

Ошибки:
SearchLabError — пул не отдал соединение или база недоступна.
IxSearchError — режим не обслужен, фильтр называет неизвестное имя, запрос отклонён.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Sequence
from enum import StrEnum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

import psycopg
from pydantic import Field

from boba.config import ConfigError, bind_section
from boba.db.postgres import AsyncPostgresPool, PostgresError
from boba.ix_core.database import IxDatabase, IxDatabaseError, IxPool
from boba.ix_core.search import (
    IxSearch,
    IxSearchError,
    SearchMode,
    SearchRegistry,
    SearchReply,
    SearchRequest,
)
from boba.ix_core.surfaces import Surface
from boba.llm.embedding import Embedder, EmbedderFactory, LocalEmbedding

logger = logging.getLogger("ix-search-lab")


class SearchLabError(Exception):
    """Ошибка стенда поиска."""


class Route(StrEnum):
    PAGE = "/"
    SEARCH = "/search"
    SURFACES = "/surfaces"


class LabConfig(IxDatabase):
    cache_dir: str
    model: str = "intfloat/multilingual-e5-large"
    dim: int = Field(gt=0, default=1024)
    host: str = "127.0.0.1"
    port: int = Field(gt=0, default=8700)


class Searcher:
    """Обслуживание запроса страницы: вектор своей моделью, соединение из пула,
    поиск ядром. Поток сервера зовёт search_from_thread, который отдаёт корутину
    в loop пула."""

    def __init__(
        self,
        cfg: LabConfig,
        pool: AsyncPostgresPool,
        embedder: Embedder[str],
        registry: SearchRegistry,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._cfg = cfg
        self._pool = pool
        self._embedder = embedder
        self._registry = registry
        self._loop = loop
        self._search = IxSearch(cfg.db_schema, registry)

    def surfaces(self) -> Sequence[Surface]:
        """Что предложить для выбора: поверхности, попадающие в индексы."""
        return self._registry.surfaces.indexed()

    def search_from_thread(
        self, mode: SearchMode, query: str, limit: int, surfaces: Sequence[str]
    ) -> SearchReply:
        future = asyncio.run_coroutine_threadsafe(
            self.search(mode, query, limit, surfaces), self._loop
        )

        return future.result()

    async def search(
        self, mode: SearchMode, query: str, limit: int, surfaces: Sequence[str]
    ) -> SearchReply:
        vector: tuple[float, ...] = ()
        if mode is SearchMode.VECTOR:
            vector = tuple(await self._embedder.embed_query(query))

        request = SearchRequest(
            mode=mode,
            query=query,
            limit=limit,
            surfaces=tuple(surfaces),
            vector=vector,
        )

        try:
            async with self._pool.connection() as conn:
                return await self._search.search(conn, request)
        except (psycopg.Error, PostgresError) as exc:
            raise SearchLabError(
                f"search {mode} in {self._cfg.postgres.where()}: no connection "
                f"from the pool: {exc}"
            ) from exc


class Handler(BaseHTTPRequestHandler):
    """Маршруты: / отдаёт страницу, /surfaces отдаёт словарь поверхностей,
    /search?q=&mode=&limit=&surface=&surface= отдаёт JSON выдачи."""

    searcher: Searcher
    page: Path

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == Route.PAGE:
            self._send(
                HTTPStatus.OK, "text/html; charset=utf-8", self.page.read_bytes()
            )
            return

        if url.path == Route.SURFACES:
            self._surfaces()
            return

        if url.path == Route.SEARCH:
            self._search(parse_qs(url.query))
            return

        self._send(HTTPStatus.NOT_FOUND, "text/plain", b"not found")

    def _surfaces(self) -> None:
        """Словарь поверхностей: по нему страница рисует выбор."""
        found: list[dict[str, object]] = []
        for surface in self.searcher.surfaces():
            found.append(surface.model_dump(mode="json"))

        self._json({"surfaces": found})

    def _search(self, args: dict[str, list[str]]) -> None:
        query = args.get("q", [""])[0].strip()
        mode_name = args.get("mode", [SearchMode.FTS.value])[0]
        limit = int(args.get("limit", ["10"])[0])
        surfaces = args.get("surface", [])
        if not query:
            self._json({"mode": mode_name, "hits": []})
            return

        try:
            mode = SearchMode(mode_name)
            reply = self.searcher.search_from_thread(mode, query, limit, surfaces)
        except (SearchLabError, IxSearchError, ValueError) as exc:
            logger.error("%s", exc)
            self._json({"mode": mode_name, "hits": [], "error": str(exc)})
            return

        self._json(reply.model_dump(mode="json"))

    def _json(self, payload: dict[str, object]) -> None:
        self._send(
            HTTPStatus.OK,
            "application/json; charset=utf-8",
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    def _send(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, log_format: str, *args: object) -> None:
        logger.info("%s %s", self.address_string(), log_format % args)


class LabServer:
    """Жизненный цикл стенда: пул к ix открыт на время работы http-сервера,
    сервер крутится в потоке исполнителя, loop главного потока обслуживает поиск."""

    def __init__(self, cfg: LabConfig, package_dir: Path) -> None:
        self._cfg = cfg
        self._dir = package_dir

    async def _registry(self, pool: AsyncPostgresPool) -> SearchRegistry:
        async with pool.connection() as conn:
            registry = await SearchRegistry.load(conn, self._cfg.db_schema)

        for table in registry.all_tables():
            logger.info("index table of kind %s: %s", table.kind, table.name)

        names: list[str] = []
        for surface in registry.surfaces.indexed():
            names.append(surface.name)

        logger.info("surfaces to search over: %s", ", ".join(names))
        logger.info("url templates for surfaces: %s", ", ".join(registry.urls.surfaces()))

        return registry

    async def serve(self) -> None:
        embedding = LocalEmbedding(
            kind="local",
            model=self._cfg.model,
            cache_dir=self._cfg.cache_dir,
            dim=self._cfg.dim,
            batch_size=8,
            progress_every=8,
        )
        embedder = EmbedderFactory.build(embedding)

        async with IxPool.opened(self._cfg) as pool:
            registry = await self._registry(pool)
            Handler.searcher = Searcher(
                self._cfg, pool, embedder, registry, asyncio.get_running_loop()
            )
            Handler.page = self._dir / "index.html"
            server = ThreadingHTTPServer((self._cfg.host, self._cfg.port), Handler)
            logger.info("listening on http://%s:%d/", self._cfg.host, self._cfg.port)

            try:
                await asyncio.to_thread(server.serve_forever)
            finally:
                server.shutdown()
                server.server_close()


class Cli:
    """Запуск с одним аргументом --config: секция [ix.search_lab] в модель."""

    SECTION: ClassVar[str] = "ix.search_lab"

    @classmethod
    def parse(cls, argv: Sequence[str] | None = None) -> LabConfig:
        parser = argparse.ArgumentParser(
            prog="boba-ix-search-lab",
            description="Стенд поисковой выдачи ix: страница и http-сервер.",
        )
        parser.add_argument(
            "--config",
            required=True,
            type=Path,
            help=(
                "Путь к файлу конфига приложения (toml). Адрес страницы, база ix и "
                "модель эмбеддингов берутся из секции [ix.search_lab]."
            ),
        )
        args = parser.parse_args(argv)

        return bind_section(args.config, cls.SECTION, LabConfig)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    try:
        cfg = Cli.parse()
        here = Path(__file__).resolve().parent
        asyncio.run(LabServer(cfg, here).serve())
    except (ConfigError, SearchLabError, IxDatabaseError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
