"""Стенд поисковой выдачи: страница с полем запроса, три колонки результатов (fts,
trgm, vector) и подсказки при наборе (suggest: btree по префиксу и триграммы) поверх
схемы ix. Один процесс на стандартном http.server: отдаёт index.html и /search.
SQL запросов лежит в sql/ и читается на каждый запрос, чтобы править ранжирование без
перезапуска. Вектор запроса считает провайдер проекта boba.llm.embedding.

Пул к ix и эмбеддер живут в event loop главного потока; http-сервер отвечает из
своих потоков и отдаёт корутину поиска в этот loop.

Ошибки:
SearchLabError — база недоступна, файл запроса не найден или режим поиска неизвестен.
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
from pydantic import BaseModel, Field

from boba.config import ConfigError, bind_section
from boba.db.postgres import AsyncPostgresPool, PostgresError
from boba.llm.embedding import Embedder, EmbedderFactory, LocalEmbedding
from boba.pg_ix_core.database import IxDatabase, IxDatabaseError, IxPool
from boba.pg_ix_core.schema_name import SchemaName

logger = logging.getLogger("pg-search-lab")


class SearchLabError(Exception):
    """Ошибка стенда поиска."""


class Mode(StrEnum):
    FTS = "fts"
    TRGM = "trgm"
    VECTOR = "vector"
    SUGGEST = "suggest"

    def sql_file(self) -> str:
        return f"{self.value}.sql"


class Route(StrEnum):
    PAGE = "/"
    SEARCH = "/search"


class LabConfig(IxDatabase):
    cache_dir: str
    model: str = "intfloat/multilingual-e5-large"
    dim: int = Field(gt=0, default=1024)
    host: str = "127.0.0.1"
    port: int = Field(gt=0, default=8700)


class Hit(BaseModel):
    surface: str
    address: dict[str, object]
    score: float
    aspect: str
    snippet: str


class SearchReply(BaseModel):
    mode: Mode
    hits: Sequence[Hit]


class Searcher:
    """Выполняет запрос выбранного режима: SQL из sql/<mode>.sql, параметры q, limit
    и v. Соединение берётся из пула на запрос; поток сервера зовёт
    search_from_thread, который отдаёт корутину в loop пула."""

    def __init__(
        self,
        cfg: LabConfig,
        sql_dir: Path,
        embedder: Embedder[str],
        pool: AsyncPostgresPool,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._cfg = cfg
        self._dir = sql_dir
        self._embedder = embedder
        self._pool = pool
        self._loop = loop

    def search_from_thread(self, mode: Mode, query: str, limit: int) -> SearchReply:
        future = asyncio.run_coroutine_threadsafe(
            self.search(mode, query, limit), self._loop
        )

        return future.result()

    async def search(self, mode: Mode, query: str, limit: int) -> SearchReply:
        params: dict[str, object] = {"q": query, "limit": limit}
        if mode is Mode.VECTOR:
            vector = await self._embedder.embed_query(query)
            params["v"] = "[" + ",".join(f"{value:.6g}" for value in vector) + "]"

        sql_path = self._dir / mode.sql_file()
        if not sql_path.exists():
            raise SearchLabError(f"search {mode}: query file {sql_path} not found")

        text = SchemaName.render(
            sql_path.read_text(encoding="utf-8"), self._cfg.db_schema
        )

        try:
            async with self._pool.connection() as conn:
                cur = await conn.execute(text, params)
                rows = await cur.fetchall()
        except (psycopg.Error, PostgresError) as exc:
            msg = f"search {mode} in {self._cfg.postgres.where()}: {exc}"
            raise SearchLabError(msg) from exc

        hits: list[Hit] = []
        for surface, address, score, aspect, snippet in rows:
            hits.append(
                Hit(
                    surface=str(surface),
                    address=address,
                    score=float(score),
                    aspect=str(aspect),
                    snippet=str(snippet),
                )
            )

        return SearchReply(mode=mode, hits=hits)


class Handler(BaseHTTPRequestHandler):
    """Маршруты: / отдаёт страницу, /search?q=&mode=&limit= отдаёт JSON."""

    searcher: Searcher
    page: Path

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == Route.PAGE:
            self._send(
                HTTPStatus.OK, "text/html; charset=utf-8", self.page.read_bytes()
            )
            return
        if url.path == Route.SEARCH:
            self._search(parse_qs(url.query))
            return
        self._send(HTTPStatus.NOT_FOUND, "text/plain", b"not found")

    def _search(self, args: dict[str, list[str]]) -> None:
        query = args.get("q", [""])[0].strip()
        mode_name = args.get("mode", [Mode.FTS.value])[0]
        limit = int(args.get("limit", ["10"])[0])
        if not query:
            self._json({"mode": mode_name, "hits": []})
            return
        try:
            mode = Mode(mode_name)
            reply = self.searcher.search_from_thread(mode, query, limit)
        except (SearchLabError, ValueError) as exc:
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
            Handler.searcher = Searcher(
                self._cfg,
                self._dir / "sql",
                embedder,
                pool,
                asyncio.get_running_loop(),
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
            prog="boba-pg-search-lab",
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
