"""Стенд поисковой выдачи: страница с полем запроса, три колонки результатов (fts, trgm, vector)
и подсказки при наборе (suggest: btree по префиксу и триграммы) поверх схемы ix. Один процесс на стандартном http.server: отдаёт index.html и /search.
SQL запросов лежит в sql/ и читается на каждый запрос, чтобы править ранжирование без
перезапуска. Вектор запроса считает провайдер проекта boba.llm.embedding.

Ошибки:
SearchLabError — база недоступна, файл запроса не найден или режим поиска неизвестен.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from enum import StrEnum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence
from urllib.parse import parse_qs, urlparse

import psycopg
from pydantic import BaseModel, Field

from boba.llm.embedding import Embedder, EmbedderFactory, LocalEmbedding

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


class LabConfig(BaseModel):
    dsn: str
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
    """Выполняет запрос выбранного режима: SQL из sql/<mode>.sql, параметры q, limit и v."""

    def __init__(self, cfg: LabConfig, sql_dir: Path, embedder: Embedder[str]) -> None:
        self._cfg = cfg
        self._dir = sql_dir
        self._embedder = embedder

    def search(self, mode: Mode, query: str, limit: int) -> SearchReply:
        params: dict[str, object] = {"q": query, "limit": limit}
        if mode is Mode.VECTOR:
            vector = asyncio.run(self._embedder.embed_query(query))
            params["v"] = "[" + ",".join(f"{value:.6g}" for value in vector) + "]"
        sql_path = self._dir / mode.sql_file()
        if not sql_path.exists():
            raise SearchLabError(f"search {mode}: query file {sql_path} not found")
        text = sql_path.read_text(encoding="utf-8").encode("utf-8")
        try:
            with psycopg.connect(self._cfg.dsn, application_name="pg-search-lab") as conn:
                rows = conn.execute(text, params).fetchall()
        except psycopg.Error as exc:
            raise SearchLabError(f"search {mode} in {self._cfg.dsn}: {exc}") from exc
        hits: list[Hit] = []
        for surface, address, score, aspect, snippet in rows:
            hits.append(Hit(surface=str(surface), address=address, score=float(score), aspect=str(aspect), snippet=str(snippet)))
        return SearchReply(mode=mode, hits=hits)


class Handler(BaseHTTPRequestHandler):
    """Маршруты: / отдаёт страницу, /search?q=&mode=&limit= отдаёт JSON."""

    searcher: Searcher
    page: Path

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == Route.PAGE:
            self._send(HTTPStatus.OK, "text/html; charset=utf-8", self.page.read_bytes())
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
            reply = self.searcher.search(mode, query, limit)
        except (SearchLabError, ValueError) as exc:
            logger.error("%s", exc)
            self._json({"mode": mode_name, "hits": [], "error": str(exc)})
            return
        self._json(reply.model_dump(mode="json"))

    def _json(self, payload: dict[str, object]) -> None:
        self._send(HTTPStatus.OK, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _send(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        logger.info("%s %s", self.address_string(), format % args)


class Cli:
    """Аргументы командной строки в LabConfig."""

    @classmethod
    def parse(cls, argv: Sequence[str] | None = None) -> LabConfig:
        parser = argparse.ArgumentParser(description="pg-search-lab: страница проверки поисковой выдачи ix")
        parser.add_argument("--dsn", required=True, help="Строка подключения к базе ix (host=... dbname=... user=... password=...).")
        parser.add_argument("--cache-dir", required=True, help="Каталог с весами fastembed для вектора запроса, как у pg-indexer-vector.")
        parser.add_argument("--model", default="intfloat/multilingual-e5-large", help="Модель эмбеддингов; та же, что у индексатора.")
        parser.add_argument("--dim", type=int, default=1024, help="Размерность вектора, как у таблицы pg_emb_e5_1024.")
        parser.add_argument("--host", default="127.0.0.1", help="Адрес, на котором слушать.")
        parser.add_argument("--port", type=int, default=8700, help="Порт страницы.")
        args = parser.parse_args(argv)
        return LabConfig(dsn=args.dsn, cache_dir=args.cache_dir, model=args.model, dim=args.dim, host=args.host, port=args.port)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = Cli.parse()
    here = Path(__file__).resolve().parent
    embedding = LocalEmbedding(kind="local", model=cfg.model, cache_dir=cfg.cache_dir, dim=cfg.dim, batch_size=8, progress_every=8)
    Handler.searcher = Searcher(cfg, here / "sql", EmbedderFactory.build(embedding))
    Handler.page = here / "index.html"
    server = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    logger.info("listening on http://%s:%d/", cfg.host, cfg.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
