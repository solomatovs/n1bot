"""KB search tools: дискриминатор коллекции собирает строку из kb_chunks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from boba.indexing import MetadataKey, ReaderKeys, SectionKeys
from boba.kbdoc import KbDocKeys
from boba.tool.kb.caller import KbCaller
from boba.tool.kb.confluence.models import ConfluenceKeys
from boba.tool.kb.kb import (
    PostgresKnowledgeBase,
    PostgresKnowledgeBaseConfig,
)
from boba.tool.kb.models import KnowledgeBaseError, SearchHit
from boba.tool.kb.protocol import KbSearchMethod
from boba.toolkit.result import (
    ErrorResult,
    TableResult,
    ToolResult,
)

__all__ = [
    "CollectionSearch",
    "ConfluenceCollection",
    "KbDocCollection",
    "KbSearch",
    "MetaField",
]


@dataclass(frozen=True)
class MetaField:
    """Output-колонка ← ключ kb_chunks.metadata (та же MetadataKey, что у ingest)."""

    column: str
    key: MetadataKey[Any]


class CollectionSearch:
    """База дискриминатора: подкласс задаёт COLLECTION и META_FIELDS, строку — row()."""

    COLLECTION: ClassVar[str]
    META_FIELDS: ClassVar[tuple[MetaField, ...]]

    @classmethod
    def row(cls, hit: SearchHit) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": hit.id,
            "distance": hit.distance,
            "snippet": hit.snippet,
            "tags": ", ".join(sorted(hit.tags)),
        }
        for field in cls.META_FIELDS:
            row[field.column] = hit.metadata.get(field.key.name, "")
        return row


class ConfluenceCollection(CollectionSearch):
    """Коллекция Confluence-страниц: META_FIELDS — ключи confluence-ingest'а."""

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
    """Коллекция KbDoc-выгрузок; wire-имена те же, что у confluence-коллекции."""

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
    """Единый прогон KB-поиска: method выбирает канал, collection — scope."""

    TOOL_DESC: ClassVar[str] = (
        "Search the Confluence collection of the knowledge base; the method "
        "argument picks the search channel.\n\nReturns a hits table with "
        "id/distance/link/snippet columns and metadata, ordered by relevance."
    )
    METHOD_DESC: ClassVar[str] = (
        "Search channel. vector — semantic search over cosine embeddings: it "
        "catches synonyms and paraphrases, use it for long or vague wording "
        "(default). fts — lexical search over the text index (`ts_rank_cd`): "
        "it matches exact words, names and identifiers."
    )
    QUERY_DESC: ClassVar[str] = (
        "Search query. With method=vector write it as plain natural language. "
        "With method=fts write it in websearch syntax: space = AND, `OR` = "
        'alternatives, `"phrase"` = the whole phrase, `-word` = exclude.'
    )
    TOPK_DESC: ClassVar[str] = "How many hits to return. Defaults to 5."
    SNIPPET_DEFAULT: ClassVar[int] = 3000
    SNIPPET_DESC: ClassVar[str] = (
        "Maximum snippet length of a hit, in characters. Defaults to 3000."
    )

    @staticmethod
    def run(  # noqa: PLR0913
        cfg: PostgresKnowledgeBaseConfig,
        caller: KbCaller,
        collection: type[CollectionSearch],
        query: str,
        method: KbSearchMethod,
        top_k: int,
        snippet_chars: int,
    ) -> ToolResult:
        kb = PostgresKnowledgeBase(cfg=cfg, caller=caller)

        try:
            hits = kb.search(
                method=method,
                collections=[collection.COLLECTION],
                query=query,
                top_k=top_k,
                snippet_chars=snippet_chars,
            )

            rows: list[dict[str, Any]] = []
            for hit in hits:
                rows.append(collection.row(hit))
        except KnowledgeBaseError as e:
            return ErrorResult(message=str(e), error_kind="kb_search_failed")

        note = None
        if not rows:
            note = "nothing found"

        return TableResult(rows=rows, note=note)
