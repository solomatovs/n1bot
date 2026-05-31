"""CLI-runner: рендер `kb_search_*` SQL-шаблона в готовый-для-psql вид.

Что делает:

1. По полю `template` загружает соответствующий tool-конфиг:
   `vector` → `KbSearchVectorConfig` (`[tool.kb.search.vector]`),
   `fts`    → `KbSearchFtsConfig`    (`[tool.kb.search.fts]`).
2. Для `vector` строит embedder (`build_embedder(cfg.embedding)`)
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

Применение:

    BOBA_CONFIG_PATH=./local/config.toml \\
        .venv/bin/python -m boba.tool.kb.cli.search.render \\
        --template vector --query "как оформить возврат" --top-k 5

    # пайп в psql:
    BOBA_CONFIG_PATH=./local/config.toml \\
        .venv/bin/python -m boba.tool.kb.cli.search.render \\
            --template fts --query "auth middleware" \\
        | psql "$PG_DSN"

Опции (CLI-флаги через `use_cli=True`):
    --template {vector|fts}          какой шаблон рендерить
    --query <text>                   текст поискового запроса
    --top-k <int>                    сколько hits (default 5)

Все остальные параметры (collections, snippet_chars, путь
к SQL-шаблону) берутся из секций `[tool.kb.search.<template>]`.
"""

from __future__ import annotations

import logging
import sys
from typing import Annotated, Literal, LiteralString, cast

from psycopg import sql
from pydantic import Field

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.core.embedder_factory import build_embedder
from boba.tool.kb.core.postgres_pool import open_kb_pool
from boba.tool.kb.core.tools.search.fts import KbSearchFtsConfig
from boba.tool.kb.core.tools.search.vector import KbSearchVectorConfig

__all__ = ["SearchRenderCliConfig", "main"]

logger = logging.getLogger("boba.tool.kb.cli.search.render")

_CONFIGS = {
    "vector": KbSearchVectorConfig,
    "fts": KbSearchFtsConfig,
}


class SearchRenderCliConfig(BobaFlatSettings):
    """Self-contained CLI-конфиг рендерера SQL-шаблонов.

    Config-секция: `[cli.kb.search.render]`. Только runner-specific поля
    (template/query/top_k) — все search-параметры тянутся из
    `[tool.kb.search.<template>]` через одноимённый tool-конфиг при
    instantiation в `main()`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="cli.kb.search.render",
        use_cli=True,
    )

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


def main() -> int:
    # Логи в stderr — stdout оставляем для чистого SQL-вывода (pipe в psql).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    cfg = SearchRenderCliConfig()  # pyright: ignore[reportCallIssue]
    tool_cfg = _CONFIGS[cfg.template]()  # pyright: ignore[reportCallIssue]
    kb_cfg = tool_cfg.knowledge_base

    logger.info(
        "rendering template=%s query=%r top_k=%d collections=%s",
        cfg.template,
        cfg.query,
        cfg.top_k,
        list(tool_cfg.collections),
    )

    if cfg.top_k > tool_cfg.max_top_k:
        logger.warning(
            "top_k=%d превышает max_top_k=%d из [tool.kb.search.%s]; "
            "runtime-tool откажется, но рендеру это не мешает.",
            cfg.top_k,
            tool_cfg.max_top_k,
            cfg.template,
        )

    # FTS не нуждается в embedding'е — экономим embedder.embed_query().
    needs_embedding = cfg.template != "fts"
    embedder = build_embedder(kb_cfg.embedding) if needs_embedding else None

    identifiers: dict[str, sql.Composable] = {
        "chunks_table": kb_cfg.tables.chunks_ident(),
        "schema": kb_cfg.tables.schema_ident(),
    }
    if embedder is not None:
        identifiers["dim"] = sql.Literal(embedder.dim())

    params: dict[str, object] = {
        "collections": list(tool_cfg.collections),
        "query": cfg.query,
        "snippet_chars": kb_cfg.snippet_chars,
        "top_k": cfg.top_k,
    }
    if embedder is not None:
        params["embedding"] = list(embedder.embed_query(cfg.query))

    template_text = tool_cfg.search_sql_path.read_text(encoding="utf-8")
    composed = sql.SQL(cast(LiteralString, template_text)).format(**identifiers)

    # `pool.client_cursor()` отдаёт psycopg3 ClientCursor — `cur.mogrify()`
    # инлайнит bind'ы (list[float]/list[str]/int/...) с правильным
    # квотингом через psycopg type-адаптеры, ничего не выполняя на сервере.
    # Коннект берётся из process-singleton pool'а (тот же, что runtime).
    pool = open_kb_pool(kb_cfg.connection)
    with pool.client_cursor() as cur:
        rendered = cur.mogrify(composed, params)

    sys.stdout.write(rendered)
    if not rendered.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
