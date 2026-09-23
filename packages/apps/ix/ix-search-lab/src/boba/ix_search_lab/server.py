"""Стенд поисковой выдачи: страница с полем запроса, три колонки результатов (fts,
trgm, vector) и подсказки при наборе (suggest) поверх схемы ix. Один процесс на
стандартном http.server: отдаёт index.html, /surfaces и /search.

Сам поиск живёт в ядре (boba.ix_core.search): стенд при старте читает реестры схемы
в IxRegistry, а на запрос берёт соединение из пула, считает вектор своей
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
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import psycopg
from pydantic import Field

from boba.config import ConfigError, bind_section
from boba.db.postgres import AsyncPostgresPool, PostgresError
from boba.ix_core.database import IxDatabase, enter_kerberos
from boba.ix_core.registry import IxRegistry
from boba.ix_core.search import (
    Hit,
    IxSearch,
    IxSearchError,
    SearchMode,
    SearchRequest,
)
from boba.ix_core.surfaces import Surface
from boba.llm.embedding import Embedder, EmbedderFactory, LocalEmbedding

logger = logging.getLogger("ix-search-lab")


class SearchLabError(Exception):
    """Ошибка стенда поиска."""


class LabConfig(IxDatabase):
    cache_dir: str
    model: str = "intfloat/multilingual-e5-large"
    dim: int = Field(gt=0, default=1024)
    host: str = "127.0.0.1"
    port: int = Field(gt=0, default=8700)
    prefix_url: str | None = Field(default=None)


class Searcher:
    """Обслуживание запроса страницы: вектор своей моделью, соединение из пула,
    поиск ядром. Поток сервера зовёт search_from_thread, который отдаёт корутину
    в loop пула."""

    def __init__(
        self,
        cfg: LabConfig,
        pool: AsyncPostgresPool,
        embedder: Embedder[str],
        registry: IxRegistry,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._cfg = cfg
        self._pool = pool
        self._embedder = embedder
        self._registry = registry
        self._loop = loop
        self._search = IxSearch(registry)

    def surfaces(self) -> Sequence[Surface]:
        """Что предложить для выбора: поверхности, попадающие в индексы."""
        return self._registry.indexed_surfaces()

    def search_from_thread(
        self, mode: SearchMode, query: str, limit: int, surfaces: Sequence[str]
    ) -> list[Hit]:
        future = asyncio.run_coroutine_threadsafe(
            self.search(mode, query, limit, surfaces), self._loop
        )

        return future.result()

    async def search(
        self, mode: SearchMode, query: str, limit: int, surfaces: Sequence[str]
    ) -> list[Hit]:
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
                hits: list[Hit] = []
                async for hit in self._search.search(conn, request):
                    hits.append(hit)

                return hits
        except (psycopg.Error, PostgresError) as exc:
            raise SearchLabError(
                f"search {mode} in {self._cfg.postgres.where()}: no connection "
                f"from the pool: {exc}"
            ) from exc


class MyHandler(BaseHTTPRequestHandler):
    """Маршруты:
    / - отдаёт страницу,
    /surfaces - отдаёт словарь поверхностей,
    /search?q=&mode=&limit=&surface=&surface= - отдаёт JSON выдачи
    """

    searcher: Searcher
    page: Path
    prefix_url: str

    def get_page(self):
        return f"{self.prefix_url}/"

    def get_search(self):
        return f"{self.prefix_url}/search"

    def get_surfaces(self):
        return f"{self.prefix_url}/surfaces"

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if self.prefix_url:
            if not url.path.startswith(self.prefix_url):
                self._send(HTTPStatus.NOT_FOUND, "text/plain", b"not found")

        if url.path == self.get_page():
            self._send(
                HTTPStatus.OK, "text/html; charset=utf-8", self.page.read_bytes()
            )
            return

        if url.path == self.get_surfaces():
            self._surfaces()
            return

        if url.path == self.get_search():
            self._search(parse_qs(url.query))
            return

        self._send(HTTPStatus.NOT_FOUND, "text/plain", b"not found")

    def _surfaces(self) -> None:
        """Словарь поверхностей: по нему страница рисует выбор."""
        found: list[dict[str, object]] = []
        for surface in self.searcher.surfaces():
            found.append(asdict(surface))

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
            hits = self.searcher.search_from_thread(mode, query, limit, surfaces)
        except (SearchLabError, IxSearchError, ValueError) as exc:
            logger.error("%s", exc)
            self._json({"mode": mode_name, "hits": [], "error": str(exc)})
            return

        found: list[dict[str, object]] = []
        for hit in hits:
            found.append(asdict(hit))

        self._json({"mode": mode_name, "hits": found})

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

    async def _registry(self, pool: AsyncPostgresPool) -> IxRegistry:
        async with pool.connection() as conn:
            registry = await IxRegistry(self._cfg.db_schema).read(conn)

        for x in registry.get_tables():
            logger.info("index table of kind %s: %s", x.kind, x.name)

        for x in registry.indexed_surfaces():
            logger.info("surfaces to search over: %s", x.name)

        for x in registry.get_urls():
            logger.info("url templates for surfaces: %s", x)

        return registry

    def handler_bootstrap(self, pool, embedder, registry):
        MyHandler.searcher = Searcher(
            self._cfg, pool, embedder, registry, asyncio.get_running_loop()
        )
        MyHandler.page = self._dir / "index.html"
        MyHandler.prefix_url = self._cfg.prefix_url or ""

        return MyHandler

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

        pool = AsyncPostgresPool(self._cfg.postgres)
        await pool.open()
        try:
            registry = await self._registry(pool)

            server = ThreadingHTTPServer(
                server_address=(self._cfg.host, self._cfg.port),
                RequestHandlerClass=self.handler_bootstrap(pool, embedder, registry),
            )

            logger.info(
                "listening on http://%s:%d/%s",
                self._cfg.host,
                self._cfg.port,
                self._cfg.prefix_url,
            )

            try:
                await asyncio.to_thread(server.serve_forever)
            finally:
                server.shutdown()
                server.server_close()
        finally:
            await pool.close()


def parse_args(argv: Sequence[str] | None = None) -> LabConfig:
    """Запуск с одним аргументом --config: секция [ix.search_lab] в модель."""
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
    enter_kerberos(args.config)

    return bind_section(args.config, "ix.search_lab", LabConfig)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    try:
        cfg = parse_args()
        here = Path(__file__).resolve().parent
        asyncio.run(LabServer(cfg, here).serve())
    except (ConfigError, SearchLabError, PostgresError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
