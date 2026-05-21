"""Bootstrap-миграции схемы при старте плагина.

Подход: лениво применяем все `migrations/*.sql` в лексикографическом
порядке на каждом старте. Каждый SQL ОБЯЗАН быть идемпотентным
(`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS` через
`DO $$ ... $$`). Полноценная migration framework (alembic) — overkill
на этапе одной-двух структурных правок.

Per-dim HNSW-индекс на `embedding` — отдельная функция
`ensure_vector_index`, вызывается из `PostgresVectorStore` при первом
upsert в коллекцию (а не на старте — на старте мы не знаем, какие
коллекции/dim'ы будут).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "apply_bootstrap",
    "ensure_vector_index",
]

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def apply_bootstrap(conn: Any) -> None:
    """Применить все миграции из `migrations/*.sql` в alphabetical order.

    Args:
        conn: psycopg-совместимый Connection (sync). Будет вызван
            `conn.execute(sql)` для каждого файла.
    """
    if not _MIGRATIONS_DIR.is_dir():
        msg = f"migrations dir not found: {_MIGRATIONS_DIR}"
        raise RuntimeError(msg)
    files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        logger.warning("no migrations found in %s", _MIGRATIONS_DIR)
        return
    for f in files:
        sql = f.read_text(encoding="utf-8")
        logger.info("applying migration %s", f.name)
        conn.execute(sql)


def ensure_vector_index(conn: Any, *, dim: int) -> None:
    """Создать HNSW-индекс по `(embedding::vector(dim))` если ещё нет.

    pgvector не индексирует колонку типа `vector` без фиксированной
    размерности — индекс ставится на expression `embedding::vector(N)`.
    Поэтому один индекс = одна dim. Для single-model развёртывания
    (наш default) вызывается один раз. Если когда-нибудь будут разные
    модели в одной таблице — несколько индексов с разной dim сосуществуют.

    Args:
        conn: psycopg sync Connection.
        dim: размерность вектора (= cfg.embedding_dim).
    """
    if dim <= 0:
        msg = f"ensure_vector_index: dim must be positive, got {dim}"
        raise ValueError(msg)
    index_name = f"kb_chunks_embedding_hnsw_{dim}"
    # opclass `vector_cosine_ops` — cosine distance (`<=>`). Если хочется
    # L2 (`<->`) — поменять на `vector_l2_ops` и пересоздать индекс.
    sql = (
        f"CREATE INDEX IF NOT EXISTS {index_name} "
        f"ON kb_chunks USING hnsw "
        f"((embedding::vector({dim})) vector_cosine_ops)"
    )
    conn.execute(sql)
