"""CLI-runner: bootstrap KB-БД — миграции схемы + HNSW vector index.

Что делает:

1. `apply_bootstrap` — применяет все `migrations/*.sql` в alphabetical
   order (CREATE EXTENSION/TABLE/INDEX IF NOT EXISTS — идемпотентно).
2. `ensure_vector_index` — создаёт HNSW-индекс на `embedding::vector(N)`,
   где `N` = `embedder.dim()` (lazy probe реальной модели через
   embeddings API).

Когда запускать:

- Один раз после первого `docker compose up postgres`.
- Повторно после смены embedding-модели, если у новой другая dim'а
  (старый индекс остаётся и используется только при возврате к старой
  модели; новый создаётся под новую dim'у).
- После `git pull` с новой миграцией.

Применение:
    BOBA_CONFIG_PATH=./local/config.toml \\
        .venv/bin/python -m boba.tool.kb.cli.bootstrap

CLI-флагов нет — всё берётся из конфига оператора
(`[tool.kb.bootstrap]`).
"""

from __future__ import annotations

import logging
import time

from dishka.entities.component import Component

from boba.agent import AgentBuilder
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.core import providers as kb_providers
from boba.tool.kb.core.embedder_factory import build_embedder
from boba.tool.kb.core.embedding_model import EmbeddingModel
from boba.tool.kb.core.migrations import apply_bootstrap, ensure_vector_index
from boba.tool.kb.core.postgres_connection import PostgresConnection
from boba.tool.kb.core.postgres_pool import open_kb_pool
from boba.tool.kb.core.postgres_store_schema import PostgresStoreSchema

__all__ = ["BootstrapConfig", "main"]

logger = logging.getLogger("boba.tool.kb.cli.bootstrap")

_KB_COMPONENT = Component(kb_providers.__name__)


class BootstrapConfig(BobaFlatSettings):
    """
    Self-contained конфиг bootstrap-CLI
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.bootstrap",
    )

    connection: PostgresConnection
    tables: PostgresStoreSchema
    embedding: EmbeddingModel


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    builder = AgentBuilder().use_plugin(kb_providers)
    container = builder.di.build_container()

    try:
        with container() as req:
            cfg = req.get(BootstrapConfig, component=_KB_COMPONENT)

            pool = open_kb_pool(cfg.connection)
            embedder = build_embedder(cfg.embedding)
            logger.info(
                "postgres_store schema=%s chunks=%s collections=%s host=%s db=%s",
                cfg.tables.schema,
                cfg.tables.chunks_table,
                cfg.tables.collections_table,
                cfg.connection.host,
                cfg.connection.database,
            )

            start = time.monotonic()

            logger.info("step 1/2: applying migrations…")
            with pool.connection() as conn:
                apply_bootstrap(conn, schema_cfg=cfg.tables)

            dim = embedder.dim()
            logger.info("step 2/2: ensuring HNSW index for dim=%d…", dim)
            with pool.connection() as conn:
                ensure_vector_index(conn, dim=dim, schema_cfg=cfg.tables)

            logger.info("DONE in %.1fs — KB-DB ready", time.monotonic() - start)
            return 0
    except Exception:
        logger.exception("bootstrap FAILED")
        return 1
    finally:
        container.close()


if __name__ == "__main__":
    raise SystemExit(main())
