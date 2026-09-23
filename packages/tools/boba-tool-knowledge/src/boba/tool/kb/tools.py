"""Инструменты базы знаний ix: словарь того, по чему искать, три режима поиска и
чтение объекта. Функции уровня модуля, модуль — обычная программа. Имена с
суффиксом 2 отличают их от старого поиска по чанкам (kb.chunks), который живёт
рядом на период миграции.

Эмбеддинг (fastembed/ONNX) и SQL исполняются в теле — потому оно живёт в
песочнице: инференс над недоверенным текстом не идёт в процессе приложения.
Сам поиск и чтение объекта делает ядро boba.ix_core: тело открывает соединение,
читает реестры схемы и отдаёт запрос ядру.

Ошибки:
PostgresError — до базы ix не достучаться (сеть, libpq, kerberos).
psycopg.Error — СУБД отклонила запрос.
EmbeddingError — удалённый эмбеддер недоступен или ответил мусором.
IxSearchError — фильтр называет неизвестную поверхность или аспект, режим не
    обслужен ни одной таблицей реестра.
NodeReadError — объекта с таким id нет или аспект неизвестен.
Отсутствие весов локального эмбеддера ожидаемым не считается: это дефект
сборки rootfs, и трейсбек там по делу.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from boba.db.postgres import PayloadPostgres, PostgresError
from boba.ix_core.indexes import IndexKind
from boba.ix_core.nodes import NodeCard, NodeReader, NodeReadError
from boba.ix_core.registry import IxRegistry
from boba.ix_core.search import (
    Hit,
    IxSearch,
    IxSearchError,
    SearchMode,
    SearchRequest,
)
from boba.ix_core.surfaces import Surface
from boba.llm.embedding import EmbeddingConfig, EmbeddingError
from boba.llm.warm import WarmEmbedder
from boba.tool.kb.chunks import kb_fts_search, kb_vector_search
from boba.tool.kb.kb import KbToolConfig
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool, warmup
from boba.toolkit.result import MarkdownResult, TableResult
from boba.toolkit.timing import Elapsed
from boba.toolkit.types import LLMStringList

logger = logging.getLogger(__name__)


class KbWarmupConfig(BaseModel):
    """Конфиг прогрева зиготы: только эмбеддер, секреты подключения не нужны."""

    model_config = ConfigDict(extra="ignore")

    embedding: EmbeddingConfig


@warmup
async def warm_embedder(cfg: KbWarmupConfig) -> None:
    """Модель ONNX поднимается в зиготе: дети берут её через COW."""
    embedder = WarmEmbedder.load(cfg.embedding)
    await embedder.embed_query("warm-up")


class KbErrorKind(StrEnum):
    """Ожидаемые отказы инструментов kb."""

    DATABASE_UNAVAILABLE = "database_unavailable"
    QUERY_FAILED = "kb_query_failed"
    EMBEDDING_FAILED = "embedding_failed"
    SEARCH_REJECTED = "kb_search_rejected"
    NODE_NOT_FOUND = "kb_node_not_found"


class HitColumn(StrEnum):
    """Колонки строки выдачи поиска."""

    NODE_ID = "node_id"
    SURFACE = "surface"
    URL = "url"
    SCORE = "score"
    ASPECT = "aspect"
    SNIPPET = "snippet"


class CatalogColumn(StrEnum):
    """Колонки строки словаря поверхностей."""

    SURFACE = "surface"
    DESCRIPTION = "description"
    NODES = "nodes"
    ASPECTS = "aspects"


class Prompt:
    """Тексты параметров и подписей для модели."""

    QUERY_FTS: ClassVar[str] = (
        "Запрос в websearch-синтаксисе — лексический поиск по словам текста "
        "(стемминг русского и английского): пробел = AND, `OR` = альтернативы, "
        '`"фраза"` = фраза целиком, `-слово` = исключить. Для точных слов, имён, '
        "идентификаторов и терминов."
    )
    QUERY_TRGM: ClassVar[str] = (
        "Слово или короткое имя — поиск по похожести триграмм (опечатки, часть "
        "имени, другой регистр) среди идентификаторов: заголовков, имён таблиц и "
        "колонок, путей. Не для фраз и не для текста страниц."
    )
    QUERY_VECTOR: ClassVar[str] = (
        "Запрос на естественном языке — семантический поиск по эмбеддингам "
        "описаний и текстов (синонимы, перефразировки, размытые формулировки)."
    )
    SURFACES: ClassVar[str] = (
        "Виды объектов из kb_catalog2, по которым искать: "
        '["cfl_page", "cfl_attachment"] или ["pg_meta_table"]. Пусто — все.'
    )
    ASPECTS: ClassVar[str] = (
        "Аспекты (какой текст объекта) из kb_catalog2, по которым искать: "
        '["title", "body"] или ["meta_description"]. Пусто — все аспекты.'
    )
    TOP_K: ClassVar[str] = "Сколько объектов вернуть."
    NODE_ID: ClassVar[str] = "Идентификатор объекта node_id из выдачи поиска."
    NODE_ASPECTS: ClassVar[str] = (
        "Какие тексты объекта вернуть, по именам аспектов из kb_catalog2: "
        '["body"] — только текст страницы. Пусто — все тексты объекта.'
    )
    NOTHING_FOUND: ClassVar[str] = (
        "nothing found: check the surfaces and aspects with kb_catalog2, "
        "or try another search mode"
    )
    MODES: ClassVar[str] = (
        "search modes: kb_fts_search2 — words of any aspect (websearch syntax); "
        "kb_trgm_search2 — fuzzy match of identifiers (ident, words aspects); "
        "kb_vector_search2 — meaning of descriptions (description aspects). "
        "kb_node2 reads the full texts of an object by node_id."
    )


class KbSession:
    """Соединение к базе ix и реестры схемы на один вызов инструмента."""

    def __init__(
        self,
        cfg: KbToolConfig,
        conn: psycopg.AsyncConnection[Any],
        registry: IxRegistry,
    ) -> None:
        self._cfg = cfg
        self._conn = conn
        self._registry = registry

    @classmethod
    @asynccontextmanager
    async def from_cfg(cls, cfg: KbToolConfig) -> AsyncGenerator[KbSession, None]:
        elapsed_conn = Elapsed()
        conn = await PayloadPostgres.connect_config(cfg.connection)
        logger.info("ix connected in %dms", elapsed_conn.ms())

        async with conn:
            elapsed_load = Elapsed()
            registry = await IxRegistry(cfg.db_schema).read(conn)
            logger.info("ix registries loaded in %dms", elapsed_load.ms())

            yield cls(cfg, conn, registry)

    @property
    def conn(self) -> psycopg.AsyncConnection[Any]:
        return self._conn

    @property
    def registry(self) -> IxRegistry:
        return self._registry

    async def search(self, request: SearchRequest) -> Sequence[Hit]:
        if request.mode is SearchMode.VECTOR:
            # При использовании приближенных индексов запросы с фильтрацией
            # могут возвращать меньше результатов, так как фильтрация
            # применяется уже после сканирования индекса.
            # Начиная с версии 0.8.0, можно включить итеративное сканирование индекса:
            # система будет автоматически сканировать индекс до тех пор,
            # пока не наберется достаточное количество результатов
            # (или пока не будут достигнуты лимиты
            # `hnsw.max_scan_tuples` либо `ivfflat.max_probes`).
            await self._conn.execute("set hnsw.iterative_scan = strict_order")

        elapsed = Elapsed()
        hits: list[Hit] = []
        async for hit in IxSearch(self._registry).search(self._conn, request):
            hits.append(hit)

        logger.info(
            "ix %s search finished in %dms (%d hits)",
            request.mode,
            elapsed.ms(),
            len(hits),
        )

        return hits

    async def node(self, node_id: int, aspects: Sequence[str]) -> NodeCard:
        reader = NodeReader(self._registry)

        return await reader.read(self._conn, node_id, aspects)

    async def coverage(self) -> dict[tuple[str, str], list[IndexKind]]:
        """Виды индексов, в которых есть каждая пара «поверхность, аспект»."""
        found: dict[tuple[str, str], list[IndexKind]] = {}
        for table in self._registry.get_tables():
            pairs = await self._registry.read_coverage(self._conn, table)
            for pair in pairs:
                kinds = found.setdefault(pair, [])
                if table.kind in kinds:
                    continue

                kinds.append(table.kind)

        return found


class HitRows:
    """Строки выдачи поиска для таблицы ответа."""

    SNIPPET_CHARS: ClassVar[int] = 500
    ELLIPSIS: ClassVar[str] = "…"
    SCORE_DIGITS: ClassVar[int] = 4

    @classmethod
    def of(cls, hits: Sequence[Hit]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for hit in hits:
            rows.append(cls._row(hit))

        return rows

    @classmethod
    def _row(cls, hit: Hit) -> dict[str, Any]:
        where = hit.url
        if not where:
            where = json.dumps(dict(hit.address), ensure_ascii=False)

        return {
            HitColumn.NODE_ID.value: hit.node_id,
            HitColumn.SURFACE.value: hit.surface,
            HitColumn.URL.value: where,
            HitColumn.SCORE.value: round(hit.score, cls.SCORE_DIGITS),
            HitColumn.ASPECT.value: hit.aspect,
            HitColumn.SNIPPET.value: cls._snippet(hit.snippet),
        }

    @classmethod
    def _snippet(cls, text: str) -> str:
        flat = " ".join(text.split())
        if len(flat) <= cls.SNIPPET_CHARS:
            return flat

        return flat[: cls.SNIPPET_CHARS] + cls.ELLIPSIS


class CatalogRows:
    """Строки словаря: поверхность, её описание, число объектов и аспекты с классом и
    видами индексов, в которых аспект есть."""

    @classmethod
    def of(
        cls,
        surfaces: Sequence[Surface],
        registry: IxRegistry,
        coverage: Mapping[tuple[str, str], Sequence[IndexKind]],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for surface in surfaces:
            rows.append(
                {
                    CatalogColumn.SURFACE.value: surface.name,
                    CatalogColumn.DESCRIPTION.value: surface.description,
                    CatalogColumn.NODES.value: surface.nodes,
                    CatalogColumn.ASPECTS.value: cls._aspects(
                        surface.name, registry, coverage
                    ),
                }
            )

        return rows

    @classmethod
    def _aspects(
        cls,
        surface: str,
        registry: IxRegistry,
        coverage: Mapping[tuple[str, str], Sequence[IndexKind]],
    ) -> str:
        parts: list[str] = []
        for entry in registry.aspects_of(surface):
            kinds = coverage.get((surface, entry.aspect), ())
            names: list[str] = []
            for kind in kinds:
                names.append(kind.value)

            indexed = ", ".join(names)
            if not indexed:
                indexed = "not indexed"

            parts.append(f"{entry.aspect} ({entry.aspect_class}: {indexed})")

        return "; ".join(parts)

    @staticmethod
    def note(registry: IxRegistry) -> str:
        lines: list[str] = ["aspects:"]
        for entry in registry.get_aspects():
            lines.append(
                f"- {entry.aspect} [{entry.aspect_class}]: {entry.description}"
            )

        lines.append("")
        lines.append(Prompt.MODES)

        return "\n".join(lines)


class NodeMarkdown:
    """Карточка объекта в markdown в пределах потолка символов."""

    TRUNCATED: ClassVar[str] = "text truncated to {chars} chars"

    @classmethod
    def render(cls, card: NodeCard, max_chars: int) -> MarkdownResult:
        head = cls._head(card)
        budget = max_chars - len(head)

        sections: list[str] = []
        truncated = False
        for text in card.texts:
            if budget <= 0:
                truncated = True
                break

            section = f"\n## {text.aspect} ({text.aspect_class})\n\n{text.content}\n"
            if len(section) > budget:
                section = section[:budget]
                truncated = True

            sections.append(section)
            budget -= len(section)

        note = None
        if truncated:
            note = cls.TRUNCATED.format(chars=max_chars)

        return MarkdownResult(text=head + "".join(sections), note=note)

    @staticmethod
    def _head(card: NodeCard) -> str:
        lines: list[str] = [f"# {card.surface} node {card.node_id}", ""]
        if card.url:
            lines.append(f"url: {card.url}")

        lines.append(f"address: {json.dumps(dict(card.address), ensure_ascii=False)}")

        labels: list[str] = []
        for step in card.parents:
            labels.append(f"{step.label} [{step.surface} {step.node_id}]")

        if labels:
            lines.append("path: " + " / ".join(labels))

        lines.append("")

        return "\n".join(lines)


async def query_to_vector(cfg: KbToolConfig, query: str) -> tuple[float, ...]:
    """Возвращает вектор запроса пользователя"""
    build = Elapsed()
    embedder = WarmEmbedder.of(cfg.embedding)
    logger.info("embedder ready in %dms (%s)", build.ms(), cfg.embedding.kind)

    embed = Elapsed()
    vector = await embedder.embed_query(query)

    values: list[float] = []
    for item in vector:
        values.append(float(item))

    logger.info("query embedded in %dms (dim=%d)", embed.ms(), len(values))

    return tuple(values)


async def run_and_collect(
    cfg: KbToolConfig,
    request: SearchRequest,
) -> TableResult:
    async with KbSession.from_cfg(cfg) as session:
        hits = await session.search(request)

    rows = HitRows.of(hits)

    note = None
    if not rows:
        note = Prompt.NOTHING_FOUND

    return TableResult(rows=rows, note=note)


@tool
async def kb_catalog2(
    *,
    cfg: Annotated[KbToolConfig, Injected],
) -> TableResult:
    """Каталог данных по которым можно производить поиск

    Строка на вид объекта:
    - страница Confluence, вложение
    - таблица PostgreSQL, ClickHouse, колонка

    описание, число объектов и её аспекты — какие тексты объекта проиндексированы
    и в каких режимах поиска (fts, trgm, vector) они есть.
    В подписи — словарь аспектов и назначение режимов.

    Необходимо вызвать что выбрать surfaces и aspects для поиска.
    """
    async with KbSession.from_cfg(cfg) as session:
        coverage = await session.coverage()
        surfaces = session.registry.indexed_surfaces()
        rows = CatalogRows.of(surfaces, session.registry, coverage)
        note = CatalogRows.note(session.registry)

    return TableResult(rows=rows, note=note)


@tool
async def kb_fts_search2(
    query: Annotated[str, Field(min_length=1, description=Prompt.QUERY_FTS)],
    surfaces: Annotated[LLMStringList, Field(default=[], description=Prompt.SURFACES)],
    aspects: Annotated[LLMStringList, Field(default=[], description=Prompt.ASPECTS)],
    top_k: Annotated[int, Field(ge=1, description=Prompt.TOP_K)] = 10,
    *,
    cfg: Annotated[KbToolConfig, Injected],
) -> TableResult:
    """Полнотекстовый (fts) поиск по базе знаний: слова запроса в текстах объектов.

    Возвращает таблицу объектов по релевантности: node_id, surface, url, score,
    лучший aspect и сниппет. Полный текст объекта читает kb_node2 по node_id.
    """
    request = SearchRequest(
        mode=SearchMode.FTS,
        query=query,
        limit=top_k,
        surfaces=tuple(surfaces),
        aspects=tuple(aspects),
    )
    return await run_and_collect(cfg, request)


@tool
async def kb_trgm_search2(
    query: Annotated[str, Field(min_length=1, description=Prompt.QUERY_TRGM)],
    surfaces: Annotated[LLMStringList, Field(default=[], description=Prompt.SURFACES)],
    aspects: Annotated[LLMStringList, Field(default=[], description=Prompt.ASPECTS)],
    top_k: Annotated[int, Field(ge=1, description=Prompt.TOP_K)] = 10,
    *,
    cfg: Annotated[KbToolConfig, Injected],
) -> TableResult:
    """Нечёткий (trgm) поиск по идентификаторам базы знаний: заголовкам, именам
    таблиц и колонок, путям — с опечатками и по части имени.

    Возвращает таблицу объектов по похожести: node_id, surface, url, score,
    aspect и совпавший идентификатор.
    """
    request = SearchRequest(
        mode=SearchMode.TRGM,
        query=query,
        limit=top_k,
        surfaces=tuple(surfaces),
        aspects=tuple(aspects),
    )
    return await run_and_collect(cfg, request)


@tool
async def kb_vector_search2(
    query: Annotated[str, Field(min_length=1, description=Prompt.QUERY_VECTOR)],
    surfaces: Annotated[LLMStringList, Field(default=[], description=Prompt.SURFACES)],
    aspects: Annotated[LLMStringList, Field(default=[], description=Prompt.ASPECTS)],
    top_k: Annotated[int, Field(ge=1, description=Prompt.TOP_K)] = 10,
    *,
    cfg: Annotated[KbToolConfig, Injected],
) -> TableResult:
    """Семантический (vector) поиск по базе знаний: смысл запроса против описаний
    и текстов объектов.

    Возвращает таблицу объектов по близости (score — косинусное расстояние,
    меньше ближе): node_id, surface, url, aspect и ближайший фрагмент.
    """
    request = SearchRequest(
        mode=SearchMode.VECTOR,
        query=query,
        limit=top_k,
        surfaces=tuple(surfaces),
        aspects=tuple(aspects),
        vector=await query_to_vector(cfg, query),
    )
    return await run_and_collect(cfg, request)


@tool
async def kb_node2(
    node_id: Annotated[int, Field(ge=1, description=Prompt.NODE_ID)],
    aspects: Annotated[
        LLMStringList, Field(default=[], description=Prompt.NODE_ASPECTS)
    ],
    *,
    cfg: Annotated[KbToolConfig, Injected],
) -> MarkdownResult:
    """Объект базы знаний целиком: ссылка, адрес, путь по иерархии (спейс/страница,
    база/схема/таблица) и полные тексты его аспектов — markdown страницы, текст
    вложения, описание таблицы с колонками.
    """
    async with KbSession.from_cfg(cfg) as session:
        card = await session.node(node_id, aspects)

    return NodeMarkdown.render(card, cfg.max_result_chars)


EXPECTED: Mapping[type[Exception], KbErrorKind] = {
    PostgresError: KbErrorKind.DATABASE_UNAVAILABLE,
    psycopg.Error: KbErrorKind.QUERY_FAILED,
    EmbeddingError: KbErrorKind.EMBEDDING_FAILED,
    IxSearchError: KbErrorKind.SEARCH_REJECTED,
    NodeReadError: KbErrorKind.NODE_NOT_FOUND,
}

TOOLS: Final = ToolMain.toolset(
    kb_vector_search,
    kb_fts_search,
    kb_catalog2,
    kb_fts_search2,
    kb_trgm_search2,
    kb_vector_search2,
    kb_node2,
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
