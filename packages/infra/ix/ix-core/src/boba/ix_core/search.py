"""Поиск по индексам ix: режимы, реестры вызывающего, запрос и слияние выдачи.

Вызывающий (стенд, инструмент чата, api) один раз читает реестры схемы в
SearchRegistry: таблицы индексов по видам, словари поверхностей и аспектов, формулы
ссылок. Дальше IxSearch выполняет SearchRequest: файл sql/<режим>.sql уходит в
каждую таблицу нужного вида на переданном соединении, строки становятся Hit со
ссылкой по формуле владельца, выдачи таблиц сливаются по порядку режима.

Фильтры запроса это списки имён поверхностей и аспектов. Пустой список значит «все
из словаря», и в sql всегда уходит непустой список, поэтому запрос один и тот же.
Имя, которого в словаре нет, отвергается с перечнем известных: молча пустая выдача
хуже ошибки.

Вектор запроса считает вызывающий своей моделью, и она обязана совпадать с моделью
таблицы эмбеддингов: расхождение размерностей поймает база, расхождение моделей
одной размерности никто.

Ошибки:
IxSearchError — режим не обслужен ни одной таблицей реестра, фильтр называет
    неизвестную поверхность или аспект, у векторного запроса нет вектора, файл
    запроса не найден или база отклонила запрос.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from boba.ix_core.aspects import AspectCatalog
from boba.ix_core.indexes import IndexKind, IndexTable, IndexTables
from boba.ix_core.schema_name import SchemaName
from boba.ix_core.surfaces import SurfaceCatalog
from boba.ix_core.urls import SurfaceUrls

__all__ = [
    "Hit",
    "IxSearch",
    "IxSearchError",
    "Merge",
    "SearchMode",
    "SearchRegistry",
    "SearchReply",
    "SearchRequest",
    "VectorText",
]

SQL_DIR = Path(__file__).resolve().parent / "sql"


class IxSearchError(Exception):
    """Запрос поиска не выполнен."""


class SearchMode(StrEnum):
    """Режим выдачи; у каждого свой файл запроса и свой вид таблиц индекса."""

    FTS = "fts"
    TRGM = "trgm"
    VECTOR = "vector"
    SUGGEST = "suggest"

    def sql_file(self) -> str:
        return f"{self.value}.sql"

    def kind(self) -> IndexKind:
        if self is SearchMode.FTS:
            return IndexKind.FTS

        if self is SearchMode.VECTOR:
            return IndexKind.VECTOR

        return IndexKind.TRGM

    def ascending(self) -> bool:
        """Вектор ранжируется расстоянием: меньше — ближе; остальные счётом."""
        return self is SearchMode.VECTOR

    def merges_by_text(self) -> bool:
        """Подсказка это текст: одинаковые из разных таблиц складываются в одну
        строку с суммой объектов."""
        return self is SearchMode.SUGGEST


class Hit(BaseModel):
    """Строка выдачи: объект, его адрес и ссылка, счёт, лучший аспект и сниппет."""

    model_config = ConfigDict(frozen=True)

    node_id: int
    surface: str
    address: Mapping[str, Any]
    score: float
    aspect: str
    snippet: str
    objects: int = 1
    """Сколько объектов стоит за строкой: у подсказки больше одного, у выдачи один."""
    url: str = ""
    """Ссылка на объект по формуле его поверхности; пусто — формулы нет."""

    def key(self) -> tuple[str, str]:
        return (self.aspect, self.snippet)


class SearchRequest(BaseModel):
    """Один запрос поиска: режим, текст, окно и фильтры по словарям."""

    model_config = ConfigDict(frozen=True)

    mode: SearchMode
    query: str = Field(min_length=1)
    limit: int = Field(gt=0)
    surfaces: tuple[str, ...] = ()
    aspects: tuple[str, ...] = ()
    vector: tuple[float, ...] = ()
    """Вектор запроса для режима vector; остальным режимам не нужен."""


class SearchReply(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: SearchMode
    hits: Sequence[Hit]


class VectorText:
    """Вектор запроса строкой литерала pgvector: `[0.1,0.2,...]`."""

    PRECISION: ClassVar[str] = ".6g"

    @classmethod
    def render(cls, vector: Sequence[float]) -> str:
        parts: list[str] = []
        for value in vector:
            parts.append(format(value, cls.PRECISION))

        return "[" + ",".join(parts) + "]"


class Merge:
    """Слияние выдач нескольких таблиц в одну: порядок задаёт режим, подсказки
    схлопываются по тексту со сложением числа объектов."""

    @classmethod
    def of(
        cls, mode: SearchMode, parts: Sequence[Sequence[Hit]], limit: int
    ) -> list[Hit]:
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


class SearchRegistry:
    """Реестры схемы, прочитанные один раз: таблицы индексов по видам, словари
    поверхностей и аспектов, формулы ссылок."""

    def __init__(
        self,
        tables: Mapping[IndexKind, Sequence[IndexTable]],
        surfaces: SurfaceCatalog,
        aspects: AspectCatalog,
        urls: SurfaceUrls,
    ) -> None:
        self._tables = dict(tables)
        self._surfaces = surfaces
        self._aspects = aspects
        self._urls = urls

    @classmethod
    async def load(
        cls, conn: psycopg.AsyncConnection[Any], db_schema: str
    ) -> SearchRegistry:
        tables: dict[IndexKind, list[IndexTable]] = {}
        for kind in IndexKind:
            tables[kind] = []

        for table in await IndexTables.all(conn, db_schema):
            tables[table.kind].append(table)

        surfaces = await SurfaceCatalog.load(conn, db_schema)
        aspects = await AspectCatalog.load(conn, db_schema)
        urls = await SurfaceUrls.load(conn, db_schema)

        return cls(tables, surfaces, aspects, urls)

    @property
    def surfaces(self) -> SurfaceCatalog:
        return self._surfaces

    @property
    def aspects(self) -> AspectCatalog:
        return self._aspects

    @property
    def urls(self) -> SurfaceUrls:
        return self._urls

    def tables_of(self, kind: IndexKind) -> tuple[IndexTable, ...]:
        return tuple(self._tables.get(kind, ()))

    def all_tables(self) -> tuple[IndexTable, ...]:
        found: list[IndexTable] = []
        for kind in IndexKind:
            found.extend(self.tables_of(kind))

        return tuple(found)


class IxSearch:
    """Выполнение запроса выбранного режима на переданном соединении.

    Файл sql/<режим>.sql читается на каждый вызов, чтобы ранжирование правилось без
    перезапуска; имя таблицы подставляется вместо {index}. Таблиц одного вида в
    реестре может быть несколько, запрос идёт в каждую по очереди.
    """

    def __init__(
        self, db_schema: str, registry: SearchRegistry, sql_dir: Path = SQL_DIR
    ) -> None:
        self._schema = db_schema
        self._registry = registry
        self._dir = sql_dir

    async def search(
        self, conn: psycopg.AsyncConnection[Any], request: SearchRequest
    ) -> SearchReply:
        mode = request.mode
        tables = self._registry.tables_of(mode.kind())
        if not tables:
            raise IxSearchError(
                f"search {mode}: the registry {self._schema}.index_table has no "
                f"{mode.kind()} table"
            )

        if mode is SearchMode.VECTOR and not request.vector:
            raise IxSearchError(
                f"search {mode}: expected the query vector from the caller's "
                "embedder, got none"
            )

        params: dict[str, object] = {
            "q": request.query,
            "limit": request.limit,
            "surfaces": self.surfaces_of(request.surfaces),
            "aspects": self.aspects_of(request.aspects),
        }
        if mode is SearchMode.VECTOR:
            params["v"] = VectorText.render(request.vector)

        text = self._text(mode)

        parts: list[list[Hit]] = []
        for table in tables:
            parts.append(await self._one(conn, mode, table, text, params))

        return SearchReply(mode=mode, hits=Merge.of(mode, parts, request.limit))

    def surfaces_of(self, chosen: Sequence[str]) -> list[str]:
        """Поверхности запроса: выбранные или все индексируемые из словаря."""
        catalog = self._registry.surfaces
        unknown = catalog.unknown(chosen)
        if unknown:
            raise IxSearchError(
                f"search: surfaces {list(unknown)} are not declared in "
                f"{self._schema}.surface_e; known are {list(catalog.names())}"
            )

        if chosen:
            return list(chosen)

        everywhere: list[str] = []
        for surface in catalog.indexed():
            everywhere.append(surface.name)

        return everywhere

    def aspects_of(self, chosen: Sequence[str]) -> list[str]:
        """Аспекты запроса: выбранные или все из словаря."""
        catalog = self._registry.aspects
        unknown = catalog.unknown(chosen)
        if unknown:
            raise IxSearchError(
                f"search: aspects {list(unknown)} are not declared in "
                f"{self._schema}.aspect; known are {list(catalog.names())}"
            )

        if chosen:
            return list(chosen)

        return list(catalog.names())

    def _text(self, mode: SearchMode) -> str:
        sql_path = self._dir / mode.sql_file()
        if not sql_path.exists():
            raise IxSearchError(f"search {mode}: query file {sql_path} not found")

        return sql_path.read_text(encoding="utf-8")

    async def _one(
        self,
        conn: psycopg.AsyncConnection[Any],
        mode: SearchMode,
        table: IndexTable,
        text: str,
        params: Mapping[str, object],
    ) -> list[Hit]:
        query = SchemaName.render(text, self._schema, index=table.ident())
        try:
            cur = await conn.execute(query, dict(params))
            rows = await cur.fetchall()
        except psycopg.Error as exc:
            raise IxSearchError(
                f"search {mode} over {self._schema}.{table.name}: {exc}"
            ) from exc

        hits: list[Hit] = []
        for node_id, surface, address, score, aspect, snippet, objects in rows:
            hits.append(
                Hit(
                    node_id=int(node_id),
                    surface=str(surface),
                    address=address,
                    score=float(score),
                    aspect=str(aspect),
                    snippet=str(snippet),
                    objects=int(objects),
                    url=self._registry.urls.of(str(surface), address),
                )
            )

        return hits
