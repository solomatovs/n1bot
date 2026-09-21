"""Стенд поисковой выдачи: страница с полем запроса, три колонки результатов (fts,
trgm, vector) и подсказки при наборе (suggest: btree по префиксу и триграммы) поверх
схемы ix. Один процесс на стандартном http.server: отдаёт index.html и /search.

Имён таблиц индексов стенд не знает: при старте он читает реестр
{schema}.index_table и запоминает таблицы каждого вида. Запрос идёт в каждую таблицу
отдельно и параллельно, своим соединением из пула; выдачи сливаются на стороне
стенда. Поэтому новая таблица индекса появляется в поиске после перезапуска стенда и
без правок запросов. SQL лежит в sql/ и читается на каждый запрос, чтобы править
ранжирование без перезапуска; имя таблицы подставляется вместо {index}.

Поверхности выдачи выбирает человек на странице: список стенд берёт при старте из
словаря поверхностей, оставляя те, у которых объявлены аспекты, то есть те, что вообще
попадают в индекс. Выбранное уходит в запрос списком; не выбрано ничего — значит ищем
везде, и в запрос уходят все имена словаря, чтобы фильтр в sql был один и тот же.

Ссылку на объект стенд тоже не сочиняет: формулу её сборки объявляет владелец
поверхности в {schema}.surface_url, стенд читает реестр при старте и применяет шаблон
к адресу каждой строки выдачи. У поверхности без формулы ссылки нет, и это пустая
строка, а не выдумка.

Вектор запроса стенд считает своей моделью из конфига, и она обязана совпадать с той,
которой наполнена таблица эмбеддингов: у размерностей расхождение поймает сама база,
у моделей одной размерности — никто. Пока таблица вектора одна и её владелец задаёт
модель своим конфигом, совпадение обеспечивает раскладка, а не проверка в рантайме.

Пул к ix и эмбеддер живут в event loop главного потока; http-сервер отвечает из
своих потоков и отдаёт корутину поиска в этот loop.

Ошибки:
SearchLabError — база недоступна, файл запроса не найден, режим поиска неизвестен
    или в реестре нет ни одной таблицы нужного вида.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from enum import StrEnum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from boba.config import ConfigError, bind_section
from boba.db.postgres import AsyncPostgresPool, PostgresError
from boba.ix_core.database import IxDatabase, IxDatabaseError, IxPool
from boba.ix_core.indexes import IndexKind, IndexTable, IndexTables
from boba.ix_core.schema_name import SchemaName
from boba.ix_core.surfaces import Surface, SurfaceCatalog
from boba.ix_core.urls import SurfaceUrls
from boba.llm.embedding import Embedder, EmbedderFactory, LocalEmbedding

logger = logging.getLogger("ix-search-lab")


class SearchLabError(Exception):
    """Ошибка стенда поиска."""


class Mode(StrEnum):
    """Режим выдачи; у каждого свой файл запроса и свой вид таблиц индекса."""

    FTS = "fts"
    TRGM = "trgm"
    VECTOR = "vector"
    SUGGEST = "suggest"

    def sql_file(self) -> str:
        return f"{self.value}.sql"

    def kind(self) -> IndexKind:
        if self is Mode.FTS:
            return IndexKind.FTS

        if self is Mode.VECTOR:
            return IndexKind.VECTOR

        return IndexKind.TRGM

    def ascending(self) -> bool:
        """Вектор ранжируется расстоянием: меньше — ближе; остальные счётом."""
        return self is Mode.VECTOR

    def merges_by_text(self) -> bool:
        """Подсказка это текст: одинаковые из разных таблиц складываются в одну
        строку с суммой объектов."""
        return self is Mode.SUGGEST


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


class Hit(BaseModel):
    surface: str
    address: dict[str, object]
    score: float
    aspect: str
    snippet: str
    objects: int = 1
    """Сколько объектов стоит за строкой: у подсказки больше одного, у выдачи один."""
    url: str = ""
    """Ссылка на объект по формуле его поверхности; пусто — формулы нет."""

    def key(self) -> tuple[str, str]:
        return (self.aspect, self.snippet)


class SearchReply(BaseModel):
    mode: Mode
    hits: Sequence[Hit]


class Merge:
    """Слияние выдач нескольких таблиц в одну: порядок задаёт режим, подсказки
    схлопываются по тексту со сложением числа объектов."""

    @classmethod
    def of(cls, mode: Mode, parts: Sequence[Sequence[Hit]], limit: int) -> list[Hit]:
        hits: list[Hit] = []
        for part in parts:
            hits.extend(part)

        if mode.merges_by_text():
            hits = cls._by_text(hits)

        hits.sort(key=lambda hit: hit.score, reverse=not mode.ascending())

        return hits[:limit]

    @staticmethod
    def _by_text(hits: Sequence[Hit]) -> list[Hit]:
        merged: dict[tuple[str, str], Hit] = {}
        for hit in hits:
            seen = merged.get(hit.key())
            if seen is None:
                merged[hit.key()] = hit
                continue

            score = max(seen.score, hit.score)
            merged[hit.key()] = seen.model_copy(
                update={"score": score, "objects": seen.objects + hit.objects}
            )

        return list(merged.values())


class SearchBackend(BaseModel):
    """Всё, чем стенд обслуживает запрос: пул к ix, эмбеддер, таблицы индексов из
    реестра и loop, в котором они живут."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    pool: AsyncPostgresPool
    embedder: Embedder[str]
    tables: Mapping[IndexKind, Sequence[IndexTable]]
    urls: SurfaceUrls
    catalog: SurfaceCatalog
    loop: asyncio.AbstractEventLoop


class Searcher:
    """Выполняет запрос выбранного режима: SQL из sql/<mode>.sql, параметры q, limit
    и v. Таблицы индекса приходят из реестра при старте: запрос уходит в каждую
    параллельно, своим соединением из пула, и выдачи сливаются. Поток сервера зовёт
    search_from_thread, который отдаёт корутину в loop пула."""

    def __init__(self, cfg: LabConfig, sql_dir: Path, backend: SearchBackend) -> None:
        self._cfg = cfg
        self._dir = sql_dir
        self._embedder = backend.embedder
        self._pool = backend.pool
        self._loop = backend.loop
        self._tables = dict(backend.tables)
        self._urls = backend.urls
        self._catalog = backend.catalog

    def surfaces(self) -> Sequence[Surface]:
        """Что предложить для выбора: поверхности, попадающие в индексы."""
        return self._catalog.indexed()

    def search_from_thread(
        self, mode: Mode, query: str, limit: int, surfaces: Sequence[str]
    ) -> SearchReply:
        future = asyncio.run_coroutine_threadsafe(
            self.search(mode, query, limit, surfaces), self._loop
        )

        return future.result()

    async def search(
        self, mode: Mode, query: str, limit: int, surfaces: Sequence[str]
    ) -> SearchReply:
        tables = self._tables.get(mode.kind(), ())
        if not tables:
            raise SearchLabError(
                f"search {mode}: the registry {self._cfg.db_schema}.index_table "
                f"has no {mode.kind()} table this stand can read"
            )

        chosen = self._chosen(surfaces)
        params: dict[str, object] = {
            "q": query,
            "limit": limit,
            "surfaces": chosen,
        }
        if mode is Mode.VECTOR:
            vector = await self._embedder.embed_query(query)
            params["v"] = "[" + ",".join(f"{value:.6g}" for value in vector) + "]"

        text = self._text(mode)
        tasks: list[asyncio.Task[list[Hit]]] = []
        for table in tables:
            tasks.append(asyncio.create_task(self._one(mode, table, text, params)))

        parts = await asyncio.gather(*tasks)

        return SearchReply(mode=mode, hits=Merge.of(mode, parts, limit))

    def _chosen(self, surfaces: Sequence[str]) -> list[str]:
        """Поверхности запроса: выбранные страницей или все из словаря. Пустого
        списка запрос не получает, поэтому фильтр в sql один и тот же."""
        unknown = self._catalog.unknown(surfaces)
        if unknown:
            raise SearchLabError(
                f"search: surfaces {list(unknown)} are not declared in "
                f"{self._cfg.db_schema}.surface_e; known are "
                f"{list(self._catalog.names())}"
            )

        if surfaces:
            return list(surfaces)

        everywhere: list[str] = []
        for surface in self._catalog.indexed():
            everywhere.append(surface.name)

        return everywhere

    def _text(self, mode: Mode) -> str:
        sql_path = self._dir / mode.sql_file()
        if not sql_path.exists():
            raise SearchLabError(f"search {mode}: query file {sql_path} not found")

        return sql_path.read_text(encoding="utf-8")

    async def _one(
        self,
        mode: Mode,
        table: IndexTable,
        text: str,
        params: Mapping[str, object],
    ) -> list[Hit]:
        query = SchemaName.render(text, self._cfg.db_schema, index=table.ident())
        try:
            async with self._pool.connection() as conn:
                cur = await conn.execute(query, dict(params))
                rows = await cur.fetchall()
        except (psycopg.Error, PostgresError) as exc:
            msg = (
                f"search {mode} over {table.name} in "
                f"{self._cfg.postgres.where()}: {exc}"
            )
            raise SearchLabError(msg) from exc

        hits: list[Hit] = []
        for surface, address, score, aspect, snippet, objects in rows:
            hits.append(
                Hit(
                    surface=str(surface),
                    address=address,
                    score=float(score),
                    aspect=str(aspect),
                    snippet=str(snippet),
                    objects=int(objects),
                    url=self._urls.of(str(surface), address),
                )
            )

        return hits


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
        found: list[dict[str, str]] = []
        for surface in self.searcher.surfaces():
            found.append(surface.model_dump(mode="json"))

        self._json({"surfaces": found})

    def _search(self, args: dict[str, list[str]]) -> None:
        query = args.get("q", [""])[0].strip()
        mode_name = args.get("mode", [Mode.FTS.value])[0]
        limit = int(args.get("limit", ["10"])[0])
        surfaces = args.get("surface", [])
        if not query:
            self._json({"mode": mode_name, "hits": []})
            return

        try:
            mode = Mode(mode_name)
            reply = self.searcher.search_from_thread(mode, query, limit, surfaces)
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

    async def _tables(
        self, pool: AsyncPostgresPool
    ) -> dict[IndexKind, list[IndexTable]]:
        """Таблицы индексов из реестра, по видам."""
        schema = self._cfg.db_schema
        found: dict[IndexKind, list[IndexTable]] = {}
        async with pool.connection() as conn:
            for kind in IndexKind:
                found[kind] = await IndexTables.of_kind(conn, schema, kind)

        for kind, tables in found.items():
            names = ", ".join(table.name for table in tables)
            logger.info("index tables of kind %s: %s", kind, names or "нет")

        return found

    async def _catalog(self, pool: AsyncPostgresPool) -> SurfaceCatalog:
        """Словарь поверхностей: выбор на странице и проверка фильтра запроса."""
        async with pool.connection() as conn:
            catalog = await SurfaceCatalog.load(conn, self._cfg.db_schema)

        names: list[str] = []
        for surface in catalog.indexed():
            names.append(surface.name)

        logger.info("surfaces to search over: %s", ", ".join(names))

        return catalog

    async def _urls(self, pool: AsyncPostgresPool) -> SurfaceUrls:
        """Формулы ссылок из реестра: по ним выдача получает адрес объекта."""
        async with pool.connection() as conn:
            urls = await SurfaceUrls.load(conn, self._cfg.db_schema)

        logger.info("url templates for surfaces: %s", ", ".join(urls.surfaces()))

        return urls

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
            tables = await self._tables(pool)
            urls = await self._urls(pool)
            catalog = await self._catalog(pool)
            backend = SearchBackend(
                pool=pool,
                embedder=embedder,
                tables=tables,
                urls=urls,
                catalog=catalog,
                loop=asyncio.get_running_loop(),
            )
            Handler.searcher = Searcher(self._cfg, self._dir / "sql", backend)
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
