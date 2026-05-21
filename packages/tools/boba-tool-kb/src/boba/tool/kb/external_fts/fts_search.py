"""Tool: full-text поиск в одном whitelist-индексе оператора (вне kb_chunks)."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.tool.kb.external_fts.config import ExternalFtsConfig
from boba.tool.kb.external_fts.db import (
    FtsQueryError,
    IndexNotFoundError,
    PgFtsKnowledgeBase,
)
from boba.tools import FromConfig, FromDI, Scope, tool

__all__ = ["fts_search"]


@tool
def fts_search(
    index: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Имя whitelist-индекса оператора (из fts_list_indexes). "
                "ВНИМАНИЕ: это не наша KB-коллекция, не путать с параметром "
                "`collection` у kb_search — здесь индексы поверх ЧУЖИХ "
                "таблиц БД оператора."
            ),
        ),
    ],
    query: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Поисковый запрос. Поддерживается websearch-синтаксис: "
                'кавычки для фраз ("exact phrase"), OR для альтернатив, '
                "минус-слово для исключения."
            ),
        ),
    ],
    kb: Annotated[PgFtsKnowledgeBase, FromDI(Scope.APP)],
    cfg: Annotated[ExternalFtsConfig, FromConfig()],
    top_k: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Сколько hits вернуть. По умолчанию 5; жёсткий потолок "
                "задан в конфиге плагина (`max_top_k`)."
            ),
        ),
    ] = 5,
) -> list[dict[str, Any]]:
    """Чистый FTS-поиск (ts_rank_cd + ts_headline) по whitelist-индексу оператора.

    Это НЕ поиск по нашей KB (для неё — `kb_search`, hybrid vector+FTS+RRF
    по коллекциям внутри `kb_chunks`). Здесь — read-only websearch по чужой
    таблице, описанной в `[tool.kb.external_fts].indexes` как `IndexSpec`
    (`schema/table/id_column/tsv_column/snippet_column/...`). Подходит для
    подключения уже-существующих PostgreSQL-источников без миграции данных
    в нашу схему.

    Когда выбирать `fts_search` vs `kb_search`:
    - **fts_search** — оператор знает БД и сам объявил индексы; ищем
      по чужим таблицам; результат — `{id, score, metadata, snippet}`,
      где `id` — значение `id_column` (PK таблицы оператора).
    - **kb_search** — наша KB, наполненная через `kb_ingest` /
      `kb_ingest_confluence`; hybrid score (vector + FTS), коллекции
      внутри `kb_chunks`.

    Сначала вызови `fts_list_indexes`, чтобы получить список доступных
    `name` + `description`. Возвращает JSON-массив hits, упорядоченный по
    релевантности (score = ts_rank_cd, больше = ближе).
    """
    if top_k > cfg.max_top_k:
        raise RuntimeError(
            f"top_k={top_k} превышает max_top_k={cfg.max_top_k}",
        )
    try:
        hits = kb.search(index=index, query=query, top_k=top_k)
    except IndexNotFoundError as e:
        raise RuntimeError(
            f"fts index {e.name!r} not found; "
            f"call fts_list_indexes to see available ones",
        ) from e
    except FtsQueryError as e:
        raise RuntimeError(str(e)) from e

    return [
        {
            "id": h.id,
            "score": h.score,
            "metadata": dict(h.metadata),
            "snippet": h.snippet,
        }
        for h in hits
    ]
