"""Ручной прогон операций базы знаний: KbOps вызывается напрямую.

Эмбеддер грузится в процессе теста, SQL идёт в postgres из [tool.kb]. Веса
берутся с хоста: путь конфига (/var/cache/fastembed) существует только внутри
песочницы.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from boba.settings import bind
from boba.tool.kb import PostgresKnowledgeBaseConfig
from boba.tool.kb.caller import KbCaller, KbSearchRequest
from boba.tool.kb.payload import KbOps
from boba.tool.kb.search import ConfluenceCollection, KbSearch

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    QUERY: ClassVar[str] = "как настроить доступ"

    TOP_K: ClassVar[int] = 5

    SNIPPET_CHARS: ClassVar[int] = 500

    CACHE_DIR: ClassVar[Path] = (
        Path(__file__).resolve().parents[4]
        / "compose"
        / "sandbox"
        / "third"
        / "fastembed"
    )
    """Каталог весов эмбеддера на хосте: в песочницу он монтируется как cache_dir."""

    @classmethod
    def request(
        cls,
        cfg: PostgresKnowledgeBaseConfig,
        op: str,
        sql: str,
    ) -> KbSearchRequest:
        """Запрос поиска: параметры хранилища идут из конфига приложения."""
        return KbSearchRequest(
            op=op,
            connection=cfg.connection,
            sql_template=sql,
            schema_name=cfg.tables.pg_schema,
            chunks_table=cfg.tables.chunks_table,
            collections=(ConfluenceCollection.COLLECTION,),
            query=cls.QUERY,
            top_k=cls.TOP_K,
            snippet_chars=cls.SNIPPET_CHARS,
            embedding={
                "model": cfg.embedding.model,
                "cache_dir": str(cls.CACHE_DIR),
            },
        )


@pytest.fixture(scope="module")
def kb_config(raw_config):
    return bind(raw_config, path="tool.kb", model=PostgresKnowledgeBaseConfig)


async def test_run_kb_vector_search(kb_config, payload, chunks) -> None:
    request = RunArgs.request(kb_config, KbCaller.VECTOR_OP, KbSearch.VECTOR_SQL)

    trailer = await KbOps.vector_search(payload.of(request), chunks.write)

    print(chunks.rows())
    print(trailer)


async def test_run_kb_fts_search(kb_config, payload, chunks) -> None:
    request = RunArgs.request(kb_config, KbCaller.FTS_OP, KbSearch.FTS_SQL)

    trailer = await KbOps.fts_search(payload.of(request), chunks.write)

    print(chunks.rows())
    print(trailer)
