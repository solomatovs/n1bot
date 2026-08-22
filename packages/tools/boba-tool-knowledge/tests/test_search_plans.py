"""Планы kb-поиска на схеме, поднятой боевыми миграциями (pytest -m integration).

Проверяется не скорость, а форма плана: фильтр коллекции обязан уходить в
индекс, а не в Filter поверх лишних строк. Схема создаётся, наполняется и
сносится тестом; рабочая схема KB не участвует.

Ошибки: своих не выпускает; расхождение с ожиданием — падение теста.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from omegaconf import DictConfig
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row

from boba.db.pgvector.migrations import Migrations
from boba.db.pgvector.store import PostgresStoreSchema
from boba.db.postgres import AsyncPostgresPool
from boba.settings import bind
from boba.tool.kb.kb import PostgresKnowledgeBaseConfig
from boba.tool.kb.search import KbSearch

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "kb_plan_test"
"""Схема стенда: создаётся и сносится тестом."""

DIM = 32
"""Размерность векторов стенда: форма плана от неё не зависит, а HNSW строится
на порядки быстрее боевых 1024."""

ROWS = 20000
"""Строк в стенде: на паре тысяч seq scan честно дешевле любого индекса."""

BIG = "kb_confluence"
SMALL = "kb_small"
RARE = "квазарпротокол"
"""Слово десятка документов: на частом слове seq scan остаётся правильным планом."""


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture(scope="module")
def schema_cfg() -> PostgresStoreSchema:
    return PostgresStoreSchema(pg_schema=SCHEMA)


@pytest.fixture(scope="module")
async def bench(
    raw_config: DictConfig, schema_cfg: PostgresStoreSchema
) -> AsyncIterator[AsyncPostgresPool]:
    """Схема стенда: боевые миграции + данные, на которых видно выбор плана."""
    cfg = bind(raw_config, "tool.kb", PostgresKnowledgeBaseConfig)

    pool = AsyncPostgresPool(cfg.connection)
    await pool.open()
    try:
        async with pool.connection() as conn:
            await _execute(
                conn, sql.SQL("drop schema if exists {} cascade").format(_schema())
            )
            await _execute(conn, sql.SQL("create schema {}").format(_schema()))
            await Migrations.apply_bootstrap(conn, schema_cfg=schema_cfg)
            await Migrations.ensure_vector_index(conn, dim=DIM, schema_cfg=schema_cfg)
            await _fill(conn, schema_cfg)

        yield pool
    finally:
        async with pool.connection() as conn:
            await _execute(
                conn, sql.SQL("drop schema if exists {} cascade").format(_schema())
            )
        await pool.close()


def _schema() -> sql.Identifier:
    return sql.Identifier(SCHEMA)


async def _execute(conn: AsyncConnection, statement: Any, params: Any = None) -> None:
    await conn.execute(statement, params, prepare=False)


async def _fill(conn: AsyncConnection, schema_cfg: PostgresStoreSchema) -> None:
    """Строки трёх коллекций: большая, мелкая и редкое слово в части документов."""
    chunks = schema_cfg.chunks_ident()

    await _execute(
        conn,
        sql.SQL(
            """
            insert into {chunks} (
                chunk_id, collection, source_id, chunk_index,
                content_hash, raw_content, format_content, embedding, metadata
            )
            select
                'chunk-' || g,
                case when g % 10 = 0 then {small} else {big} end,
                'source-' || (g / 20),
                g % 20,
                md5(g::text),
                'raw ' || g,
                (
                    select string_agg('слово' || (s % 400), ' ')
                    from generate_series(g, g + 40) s
                )
                || case when g % 997 = 0 then ' ' || {rare} else '' end,
                (
                    select array_agg(random()::real)::vector
                    from generate_series(1, {dim})
                ),
                jsonb_build_object('reader.page_title', 'страница ' || g)
            from
                generate_series(1, {rows}) g
            """
        ).format(
            chunks=chunks,
            big=sql.Literal(BIG),
            small=sql.Literal(SMALL),
            rare=sql.Literal(RARE),
            dim=sql.Literal(DIM),
            rows=sql.Literal(ROWS),
        ),
    )
    await _execute(conn, sql.SQL("analyze {}").format(chunks))


async def _plan(conn: AsyncConnection, statement: Any, params: Any) -> str:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(statement, params, prepare=False)

        lines: list[str] = []
        for row in await cur.fetchall():
            lines.append(row["QUERY PLAN"])

        return "\n".join(lines)


async def _index_cond(plan: str) -> Sequence[str]:
    conds: list[str] = []
    for line in plan.splitlines():
        text = line.strip()
        if text.startswith("Index Cond:"):
            conds.append(text)

    return conds


class TestMigrationsBuildIndexes:
    """Миграции обязаны оставить ровно те индексы, на которые рассчитан поиск."""

    async def test_search_indexes_exist(self, bench: AsyncPostgresPool) -> None:
        async with bench.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "select indexname from pg_indexes where schemaname = %s",
                (SCHEMA,),
                prepare=False,
            )
            names = set()
            for row in await cur.fetchall():
                names.add(row["indexname"])

        expected = {
            "kb_chunks_collection_tsv_gin",
            "kb_chunks_collection_source_chunk",
            f"kb_chunks_embedding_hnsw_{DIM}",
        }
        missing = expected - names
        if missing:
            raise AssertionError(f"миграции не создали индексы {missing}: {names}")

    async def test_superseded_indexes_are_gone(self, bench: AsyncPostgresPool) -> None:
        """Индексы, чьи запросы перекрыты новыми, не должны висеть на вставках."""
        async with bench.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "select indexname from pg_indexes where schemaname = %s",
                (SCHEMA,),
                prepare=False,
            )
            names = set()
            for row in await cur.fetchall():
                names.add(row["indexname"])

        stale = {
            "kb_chunks_tsv_gin",
            "kb_chunks_collection",
            "kb_chunks_collection_source",
        } & names
        if stale:
            raise AssertionError(f"избыточные индексы остались: {stale}")


class TestFtsPlan:
    """Полнотекстовый поиск: коллекция и tsv — одним индексным условием."""

    async def test_collection_filter_is_an_index_condition(
        self, bench: AsyncPostgresPool, schema_cfg: PostgresStoreSchema
    ) -> None:
        statement = sql.SQL("explain (analyze, buffers) " + KbSearch.FTS_SQL).format(
            chunks_table=schema_cfg.chunks_ident(), schema=_schema()
        )
        async with bench.connection() as conn:
            plan = await _plan(
                conn,
                statement,
                {
                    "collections": [BIG],
                    "query": RARE,
                    "top_k": 5,
                },
            )

        if "Seq Scan" in plan:
            raise AssertionError(f"редкое слово ушло в seq scan:\n{plan}")

        if "kb_chunks_collection_tsv_gin" not in plan:
            raise AssertionError(f"составной GIN не выбран:\n{plan}")

        conds = await _index_cond(plan)
        matched = ""
        for cond in conds:
            if "collection" in cond and "tsv @@" in cond:
                matched = cond

        if not matched:
            raise AssertionError(
                f"коллекция обязана входить в Index Cond вместе с tsv:\n{plan}"
            )

    async def test_collection_rows_are_not_rechecked(
        self, bench: AsyncPostgresPool, schema_cfg: PostgresStoreSchema
    ) -> None:
        """Строки чужих коллекций не должны доезжать до Filter'а."""
        statement = sql.SQL("explain (analyze) " + KbSearch.FTS_SQL).format(
            chunks_table=schema_cfg.chunks_ident(), schema=_schema()
        )
        async with bench.connection() as conn:
            plan = await _plan(
                conn,
                statement,
                {
                    "collections": [BIG],
                    "query": RARE,
                    "top_k": 5,
                },
            )

        for line in plan.splitlines():
            text = line.strip()
            if not text.startswith("Rows Removed by Filter:"):
                continue

            removed = int(text.split(":")[1])
            if removed > 0:
                raise AssertionError(f"фильтр отбросил {removed} строк:\n{plan}")


class TestVectorPlan:
    """Векторный поиск: HNSW по выражению индекса и полная выдача top_k."""

    async def test_hnsw_is_used(
        self, bench: AsyncPostgresPool, schema_cfg: PostgresStoreSchema
    ) -> None:
        statement = sql.SQL("explain (analyze) " + KbSearch.VECTOR_SQL).format(
            dim=sql.Literal(DIM), chunks_table=schema_cfg.chunks_ident()
        )
        async with bench.connection() as conn:
            probe = await _probe(conn, schema_cfg)
            plan = await _plan(
                conn,
                statement,
                {
                    "collections": [BIG],
                    "embedding": probe,
                    "top_k": 5,
                },
            )

        if f"kb_chunks_embedding_hnsw_{DIM}" not in plan:
            raise AssertionError(f"HNSW не выбран для большой коллекции:\n{plan}")

    async def test_index_scan_carries_no_extra_filter(
        self, bench: AsyncPostgresPool, schema_cfg: PostgresStoreSchema
    ) -> None:
        """Лишнее условие поверх индексного обхода стоит дороже самого поиска.

        Фильтр по коллекции на HNSW-обходе неизбежен — индекс про коллекцию не
        знает; всё остальное обязано остаться за пределами скана.
        """
        statement = sql.SQL("explain (analyze) " + KbSearch.VECTOR_SQL).format(
            dim=sql.Literal(DIM), chunks_table=schema_cfg.chunks_ident()
        )
        async with bench.connection() as conn:
            probe = await _probe(conn, schema_cfg)
            plan = await _plan(
                conn,
                statement,
                {
                    "collections": [BIG],
                    "embedding": probe,
                    "top_k": 5,
                },
            )

        for line in plan.splitlines():
            text = line.strip()
            if not text.startswith("Filter:"):
                continue

            if "collection" in text and "embedding" not in text:
                continue

            raise AssertionError(f"на индексном обходе висит {text}:\n{plan}")

    async def test_body_is_read_only_for_hits(
        self, bench: AsyncPostgresPool, schema_cfg: PostgresStoreSchema
    ) -> None:
        """Сниппет и метаданные обязаны считаться только для выданных строк."""
        statement = sql.SQL("explain (analyze) " + KbSearch.VECTOR_SQL).format(
            dim=sql.Literal(DIM), chunks_table=schema_cfg.chunks_ident()
        )
        async with bench.connection() as conn:
            probe = await _probe(conn, schema_cfg)
            plan = await _plan(
                conn,
                statement,
                {
                    "collections": [BIG],
                    "embedding": probe,
                    "top_k": 5,
                },
            )

        if "Sort Method" in plan:
            raise AssertionError(f"выдача досортировывается в памяти:\n{plan}")

    async def test_top_k_is_complete_under_collection_filter(
        self, bench: AsyncPostgresPool, schema_cfg: PostgresStoreSchema
    ) -> None:
        """Фильтр коллекции не должен урезать выдачу приближённого поиска.

        Индекс не знает про коллекцию: он просматривает фиксированное число
        кандидатов, фильтр выбрасывает чужие, и без итеративного обхода до
        limit доживает лишь часть запрошенного.
        """
        statement = sql.SQL(KbSearch.VECTOR_SQL).format(
            dim=sql.Literal(DIM), chunks_table=schema_cfg.chunks_ident()
        )
        async with bench.connection() as conn:
            probe = await _probe(conn, schema_cfg)
            await _execute(conn, sql.SQL(KbSearch.ITERATIVE_SCAN))

            for top_k in (5, 50, 100):
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        statement,
                        {
                            "collections": [SMALL],
                            "embedding": probe,
                            "top_k": top_k,
                        },
                        prepare=False,
                    )
                    got = len(await cur.fetchall())

                if got != top_k:
                    raise AssertionError(
                        f"top_k={top_k} на мелкой коллекции вернул {got} строк"
                    )

    async def test_undersupply_without_iterative_scan(
        self, bench: AsyncPostgresPool, schema_cfg: PostgresStoreSchema
    ) -> None:
        """Проверка самого стенда: без итеративного обхода недобор обязан быть.

        Иначе тест выше проходил бы и с выключенным режимом, ничего не доказывая.
        """
        statement = sql.SQL(KbSearch.VECTOR_SQL).format(
            dim=sql.Literal(DIM), chunks_table=schema_cfg.chunks_ident()
        )
        async with bench.connection() as conn:
            probe = await _probe(conn, schema_cfg)
            await _execute(conn, "set hnsw.iterative_scan = off")

            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    statement,
                    {
                        "collections": [SMALL],
                        "embedding": probe,
                        "top_k": 100,
                    },
                    prepare=False,
                )
                got = len(await cur.fetchall())

            await _execute(conn, "reset hnsw.iterative_scan")

        if got == 100:
            raise AssertionError(
                "стенд не воспроизводит недобор: проверка полноты ничего не доказывает"
            )

    async def test_small_collection_returns_exact_hits(
        self, bench: AsyncPostgresPool, schema_cfg: PostgresStoreSchema
    ) -> None:
        """На мелкой коллекции выдача обязана совпасть с точным перебором."""
        statement = sql.SQL(KbSearch.VECTOR_SQL).format(
            dim=sql.Literal(DIM), chunks_table=schema_cfg.chunks_ident()
        )
        params = {
            "collections": [SMALL],
            "embedding": "",
            "top_k": 5,
        }

        async with bench.connection() as conn:
            params["embedding"] = await _probe(conn, schema_cfg)

            planned = await _hit_titles(conn, statement, params)

            await _execute(conn, "set local enable_indexscan = off")
            exact = await _hit_titles(conn, statement, params)
            await _execute(conn, "reset enable_indexscan")

        if planned != exact:
            raise AssertionError(f"выдача {planned} расходится с точной {exact}")


class TestListingPlan:
    """Листинги чанков: порядок берётся из индекса, а не из сортировки."""

    async def test_listing_is_not_sorted_in_memory(
        self, bench: AsyncPostgresPool, schema_cfg: PostgresStoreSchema
    ) -> None:
        statement = sql.SQL(
            """
            explain (analyze)
            select chunk_id from {chunks}
            where collection = %s and source_id = %s
            order by chunk_index
            limit 50
            """
        ).format(chunks=schema_cfg.chunks_ident())

        async with bench.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    sql.SQL(
                        "select source_id from {} where collection = %s limit 1"
                    ).format(schema_cfg.chunks_ident()),
                    (BIG,),
                    prepare=False,
                )
                row = await cur.fetchone()
                if row is None:
                    raise AssertionError("в стенде нет строк большой коллекции")

                source = row["source_id"]

            plan = await _plan(conn, statement, (BIG, source))

        if "Sort" in plan:
            raise AssertionError(f"листинг досортировывается в памяти:\n{plan}")

        if "kb_chunks_collection_source_chunk" not in plan:
            raise AssertionError(
                f"btree по (collection, source_id, chunk_index):\n{plan}"
            )


async def _probe(conn: AsyncConnection, schema_cfg: PostgresStoreSchema) -> str:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            sql.SQL("select embedding::text as vec from {} limit 1").format(
                schema_cfg.chunks_ident()
            ),
            prepare=False,
        )
        row = await cur.fetchone()
        if row is None:
            raise AssertionError("в стенде нет ни одного эмбеддинга")

        return row["vec"]


async def _hit_titles(conn: AsyncConnection, statement: Any, params: Any) -> list[str]:
    """Заголовки найденных строк: выдача больше не содержит идентификаторов."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(statement, params, prepare=False)

        found: list[str] = []
        for row in await cur.fetchall():
            found.append(str(row["metadata"]["reader.page_title"]))

        return found
