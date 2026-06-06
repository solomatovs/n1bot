"""CLI-runner: рендер `kb_search_*` SQL-шаблона в готовый-для-psql вид.

Что делает:

1. Загружает `PostgresKnowledgeBaseConfig` (`[tool.kb]`); по полю `template`
   (`vector`|`fts`) выбирает встроенный SQL-шаблон
   (`KbSearch.VECTOR_SQL`/`FTS_SQL`).
2. Для `vector` строит embedder (`cfg.embedding.build()`)
   и считает `embed_query(query)` — N-мерный вектор для cosine-поиска.
3. Подставляет identifier-placeholder'ы (`{dim}`/`{chunks_table}`/`{schema}`)
   через `psycopg.sql.SQL.format` — те же значения, что и runtime в
   [kb.py](../../core/kb.py).
4. Инлайнит bind-параметры (`collections`/`query`/`embedding`/
   `snippet_chars`/`top_k`) через `ClientCursor.mogrify` —
   psycopg корректно квотирует массивы/числа/текст.
5. Печатает готовый SQL в stdout. Логи идут в stderr — output безопасно
   пайпать в `psql`.

Когда запускать:

- Дебаг vector/fts-выдачи: что именно получает Postgres, на каких
  параметрах. Иногда планировщик ведёт себя контр-интуитивно — лучше
  скопировать SQL и прогнать вручную с `EXPLAIN ANALYZE`.
- Бенчмарки: сгенерировать SQL для серии запросов и прогнать через
  `pgbench -f` (mogrify-вывод pgbench-совместим).
- Документация: получить точный SQL для config-снапшота — приложить к
  RCA или changelog'у.

Применение (CLI-override — `key=value`, section-relative):

    BOBA_CONFIG_PATH=./local/config.toml \\
        .venv/bin/python -m boba.tool.kb.cli.search \\
        template=vector query="как оформить возврат" top_k=5

    # пайп в psql:
    BOBA_CONFIG_PATH=./local/config.toml \\
        .venv/bin/python -m boba.tool.kb.cli.search \\
            template=fts query="auth middleware" \\
        | psql "$PG_DSN"

Опции (поля секции `[cli.kb.search.render]`):
    template={vector|fts}            какой шаблон рендерить
    query=<text>                     текст поискового запроса
    top_k=<int>                      сколько hits (default 5)
    snippet_chars=<int>              длина сниппета (default 3000)

`collections` фиксированы (обе KB-коллекции), connection/tables/embedding
берутся из секции `[tool.kb]` (`PostgresKnowledgeBaseConfig`).
"""

from __future__ import annotations

import logging
import sys
from typing import Annotated, ClassVar, Literal, LiteralString, cast

from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field

from boba.settings import bind, build_app_config
from boba.tool.kb.core.kb import PostgresKnowledgeBaseConfig
from boba.tool.kb.core.postgres import KbPool
from boba.tool.kb.core.search import (
    ConfluenceCollection,
    KbDocCollection,
    KbSearch,
)

__all__ = ["SearchRenderCliConfig", "main"]

logger = logging.getLogger("boba.tool.kb.cli.search")


class SearchRenderCliConfig(BaseModel):
    """Self-contained CLI-конфиг рендерера SQL-шаблонов.

    Config-секция: `[cli.kb.search.render]`. Только runner-specific поля
    (template/query/top_k) — все search-параметры тянутся из
    `[tool.kb.search.<template>]` через одноимённый tool-конфиг при
    instantiation в `main()`. CLI-override — `key=value` (section-relative).
    """

    COLLECTIONS: ClassVar[list[str]] = [
        ConfluenceCollection.COLLECTION,
        KbDocCollection.COLLECTION,
    ]

    model_config = ConfigDict(extra="ignore")

    template: Annotated[
        Literal["vector", "fts"],
        Field(description="Какой SQL-шаблон рендерить."),
    ]
    query: Annotated[
        str,
        Field(min_length=1, description="Текст поискового запроса."),
    ]
    top_k: Annotated[
        int,
        Field(ge=1, description="Сколько hits вернуть."),
    ] = 5
    snippet_chars: Annotated[
        int,
        Field(ge=1, description="Максимальная длина сниппета (символов)."),
    ] = KbSearch.SNIPPET_DEFAULT


def main() -> int:
    # Логи в stderr — stdout оставляем для чистого SQL-вывода (pipe в psql).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    config = build_app_config(sys.argv[1:])
    cfg = bind(config, "cli.kb.search.render", SearchRenderCliConfig)
    kb_cfg = bind(config, "tool.kb", PostgresKnowledgeBaseConfig)

    logger.info(
        "rendering template=%s query=%r top_k=%d collections=%s",
        cfg.template,
        cfg.query,
        cfg.top_k,
        SearchRenderCliConfig.COLLECTIONS,
    )

    # FTS не нуждается в embedding'е — экономим embedder.embed_query().
    needs_embedding = cfg.template != "fts"
    embedder = kb_cfg.embedding.build() if needs_embedding else None

    identifiers: dict[str, sql.Composable] = {
        "chunks_table": kb_cfg.tables.chunks_ident(),
        "schema": kb_cfg.tables.schema_ident(),
    }
    if embedder is not None:
        identifiers["dim"] = sql.Literal(embedder.dim())

    params: dict[str, object] = {
        "collections": SearchRenderCliConfig.COLLECTIONS,
        "query": cfg.query,
        "snippet_chars": cfg.snippet_chars,
        "top_k": cfg.top_k,
    }
    if embedder is not None:
        params["embedding"] = list(embedder.embed_query(cfg.query))

    match cfg.template:
        case "fts":
            template_text = KbSearch.FTS_SQL
        case "vector":
            template_text = KbSearch.VECTOR_SQL
        case _:
            logger.error("unsupported template %r", cfg.template)
            return 1

    composed = sql.SQL(cast(LiteralString, template_text)).format(**identifiers)

    # `pool.client_cursor()` отдаёт psycopg3 ClientCursor — `cur.mogrify()`
    # инлайнит bind'ы (list[float]/list[str]/int/...) с правильным
    # квотингом через psycopg type-адаптеры, ничего не выполняя на сервере.
    # Коннект берётся из process-singleton pool'а (тот же, что runtime).
    pool = KbPool.open(kb_cfg.connection)
    with pool.client_cursor() as cur:
        rendered = cur.mogrify(composed, params)

    sys.stdout.write(rendered)
    if not rendered.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
