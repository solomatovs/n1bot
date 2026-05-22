"""
CLI-runner: bootstrap KB-БД — миграции схемы + HNSW vector index.

Не tool-функция, а **одноразовый операторский шаг**. Подготовка БД
вынесена из runtime (раньше `apply_bootstrap` крутилось на каждом
старте процесса в `provide_postgres_pool`, а `ensure_vector_index`
лениво на первом upsert) — оба DDL теперь стоят за этим CLI.

Что делает:

1. `apply_bootstrap` — применяет все `migrations/*.sql` в alphabetical
   order (CREATE EXTENSION/TABLE/INDEX IF NOT EXISTS — идемпотентно).
2. `ensure_vector_index` — создаёт HNSW-индекс на `embedding::vector(N)`,
   где `N` = `EmbeddingConfig.dim()` (lazy probe реальной модели через
   embeddings API).

Когда запускать:

- Один раз после первого `docker compose up postgres`.
- Повторно после смены embedding-модели, если у новой другая dim'а
  (старый индекс остаётся и используется только при возврате к старой
  модели; новый создаётся под новую dim'у).
- После `git pull` с новой миграцией.

Применение:
    BOBA_CONFIG_PATH=./local/config.toml \
        .venv/bin/python -m boba.tool.kb.core.cli.bootstrap

CLI-флагов нет — всё берётся из конфига оператора.
"""

from __future__ import annotations

import logging
import time

from dishka.entities.component import Component

from boba.agent import AgentBuilder
from boba.db.postgres import PostgresPool
from boba.indexing.embedder import Embedder
from boba.tool.kb.core import providers as kb_providers
from boba.tool.kb.core.migrations import apply_bootstrap, ensure_vector_index
from boba.tool.kb.core.vector_store_config import VectorStoreSchemaConfig

__all__ = ["main"]

logger = logging.getLogger("boba.tool.kb.core.cli.bootstrap")

_KB_COMPONENT = Component(kb_providers.__name__)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    builder = AgentBuilder().use_plugin(kb_providers)
    container = builder.di.build_container()

    try:
        with container() as req:
            pool = req.get(PostgresPool, component=_KB_COMPONENT)
            embedder = req.get(Embedder[str], component=_KB_COMPONENT)

            # `schema_cfg` грузится здесь же напрямую (BobaFlatSettings
            # самостоятельно читает из env/toml). Этот же конфиг получают
            # `PostgresVectorStore` и `PostgresKnowledgeBase` через FromConfig
            # в `provide_vector_store` / `provide_knowledge_base` — оба пути
            # дают идентичные значения (источник один). Если разъедутся —
            # bootstrap создаст одни таблицы, store будет ходить в другие.
            schema_cfg = VectorStoreSchemaConfig()
            logger.info(
                "vector_store schema=%s chunks=%s collections=%s",
                schema_cfg.schema,
                schema_cfg.chunks_table,
                schema_cfg.collections_table,
            )

            start = time.monotonic()

            logger.info("step 1/2: applying migrations…")
            with pool.connection() as conn:
                apply_bootstrap(conn, schema_cfg=schema_cfg)

            # `embedder.dim()` — lazy probe реальной модели через
            # embeddings API. Делается ровно один раз здесь, кэшируется
            # в самом embedder'е до конца процесса.
            dim = embedder.dim()
            logger.info("step 2/2: ensuring HNSW index for dim=%d…", dim)
            with pool.connection() as conn:
                ensure_vector_index(conn, dim=dim, schema_cfg=schema_cfg)

            logger.info("DONE in %.1fs — KB-DB ready", time.monotonic() - start)
            return 0
    except Exception:
        logger.exception("bootstrap FAILED")
        return 1
    finally:
        container.close()


if __name__ == "__main__":
    raise SystemExit(main())
