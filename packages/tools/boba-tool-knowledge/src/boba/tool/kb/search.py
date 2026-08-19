"""Запросы KB-поиска и разметка строк выдачи по коллекциям."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Literal, LiteralString

from boba.indexing import MetadataKey, ReaderKeys, SectionKeys
from boba.kbdoc import KbDocKeys
from boba.tool.kb.confluence.models import ConfluenceKeys
from boba.tool.kb.models import SearchHit

__all__ = [
    "CollectionSearch",
    "ConfluenceCollection",
    "KbDocCollection",
    "KbSearch",
    "MetaField",
]

SearchMethod = Literal["vector", "fts"]


@dataclass(frozen=True)
class MetaField:
    """Колонка выдачи и ключ kb_chunks.metadata, из которого она берётся."""

    column: str
    key: MetadataKey[Any]


class CollectionSearch:
    """Строка выдачи коллекции: подкласс задаёт COLLECTION и META_FIELDS."""

    COLLECTION: ClassVar[str]
    META_FIELDS: ClassVar[tuple[MetaField, ...]]

    @classmethod
    def row(cls, hit: SearchHit) -> dict[str, Any]:
        row: dict[str, Any] = {
            "distance": hit.distance,
            "format_content": hit.format_content,
        }
        for field in cls.META_FIELDS:
            row[field.column] = hit.metadata.get(field.key.name, "")
        return row


class ConfluenceCollection(CollectionSearch):
    """Коллекция Confluence-страниц; META_FIELDS — ключи confluence-ingest."""

    COLLECTION = "kb_confluence"

    META_FIELDS = (
        MetaField("page_title", ReaderKeys.PAGE_TITLE),
        MetaField("source_url", ConfluenceKeys.SOURCE_URL),
        MetaField("parent_url", ConfluenceKeys.PARENT_URL),
        MetaField("doc_type", ReaderKeys.DOC_TYPE),
        MetaField("page", SectionKeys.PAGE_NUMBER),
        MetaField("anchor", SectionKeys.ANCHOR),
        MetaField("page_id", ConfluenceKeys.PAGE_ID),
        MetaField("version", ConfluenceKeys.VERSION),
        MetaField("heading_path", SectionKeys.HEADING_PATH),
        MetaField("space", ConfluenceKeys.SPACE_KEY),
    )


class KbDocCollection(CollectionSearch):
    """Коллекция KbDoc-выгрузок из workspace; имена колонок как у confluence."""

    COLLECTION = "kb_confluence_doc"

    META_FIELDS = (
        MetaField("page_title", ReaderKeys.PAGE_TITLE),
        MetaField("source_url", KbDocKeys.SOURCE_URL),
        MetaField("parent_url", KbDocKeys.PARENT_URL),
        MetaField("doc_type", ReaderKeys.DOC_TYPE),
        MetaField("page", SectionKeys.PAGE_NUMBER),
        MetaField("anchor", SectionKeys.ANCHOR),
        MetaField("page_id", KbDocKeys.PAGE_ID),
        MetaField("version", KbDocKeys.VERSION),
        MetaField("heading_path", SectionKeys.HEADING_PATH),
        MetaField("space", KbDocKeys.SPACE),
    )


class KbSearch:
    """Запросы KB-поиска и настройки сессии под них."""

    VECTOR_SQL: ClassVar[LiteralString] = """
select
    c.metadata,
    c.format_content,
    (c.embedding::vector({dim})) <=> %(embedding)s::vector as distance
from
    {chunks_table} c
where
    c.collection = any(%(collections)s)
order by
    distance asc
limit
    %(top_k)s
"""
    """Векторный поиск.

    Из документации pgvector (https://github.com/pgvector/pgvector):

    Поддерживаются следующие функции расстояния:

    `<->` — L2-расстояние
    `<#>` — (отрицательное) скалярное произведение
    `<=>` — косинусное расстояние
    `<+>` — L1-расстояние
    `<~>` — расстояние Хэмминга (битовые векторы)
    `<%>` — расстояние Жаккара (битовые векторы)

    Примечание: `<#>` возвращает отрицательное скалярное произведение,
    поскольку Postgres поддерживает индексные сканы по операторам только в
    порядке ASC.

    Если векторы нормализованы до длины 1 (как эмбеддинги OpenAI), для
    наилучшей производительности используйте скалярное произведение.
    """

    FTS_SQL: ClassVar[LiteralString] = """
with q as (
    select
        websearch_to_tsquery('russian', {schema}.immutable_unaccent(%(query)s))
        || websearch_to_tsquery('english', {schema}.immutable_unaccent(%(query)s))
        as tsq
)
select
    c.metadata,
    c.format_content,
    ts_rank_cd(c.tsv, q.tsq) as rank
from
    {chunks_table} c,
    q
where
    c.collection = any(%(collections)s)
    and c.tsv @@ q.tsq
order by
    rank desc
limit
    %(top_k)s
"""
    """Полнотекстовый поиск."""

    ITERATIVE_SCAN: ClassVar[LiteralString] = (
        "set hnsw.iterative_scan = strict_order"
    )
    """Итеративный индексный скан.

    Из документации pgvector (https://github.com/pgvector/pgvector):

    С приближёнными индексами запросы с фильтрацией могут возвращать меньше
    результатов, поскольку фильтрация применяется после того, как индекс
    просканирован. Начиная с 0.8.0 можно включить итеративные индексные
    сканы: индекс будет сканироваться дальше, пока не наберётся достаточно
    результатов (либо пока не будет достигнут hnsw.max_scan_tuples или
    ivfflat.max_probes).

    Итеративные сканы могут использовать строгий или ослабленный порядок.

    Строгий (strict_order) гарантирует, что результаты идут в точном порядке
    по расстоянию.

    Ослабленный (relaxed_order) допускает небольшие отклонения от порядка по
    расстоянию, но даёт лучшую полноту выдачи.
    """

    QUERY_DESC_VECTOR: ClassVar[str] = (
        "Поисковый запрос на естественном языке — семантический поиск "
        "(cosine-эмбеддинги: ловит синонимы и перефразировки, хорош для "
        "длинных/размытых формулировок)."
    )
    QUERY_DESC_FTS: ClassVar[str] = (
        "Поисковый запрос в websearch-синтаксисе — лексический поиск "
        "(`ts_rank_cd`, точные слова/имена/идентификаторы): пробел = AND, "
        '`OR` = альтернативы, `"фраза"` = фраза целиком, `-слово` = исключить.'
    )
    TOPK_DESC: ClassVar[str] = "Сколько hits вернуть. По умолчанию 5."
