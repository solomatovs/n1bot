-- Multilang FTS: пересоздать tsv-колонку как concat русского и английского
-- tsvector'ов. Без этой миграции tsv хранит только russian-стемминг —
-- английские запросы попадают в FTS-канал с потерей recall.
--
-- Идемпотентность: проверяем GENERATED-выражение колонки `tsv` на наличие
-- маркера `'english'`. Если есть — миграция уже применена, выходим.
-- Иначе DROP COLUMN (зависимый GIN-индекс падает каскадом) + ADD COLUMN
-- с новым выражением + CREATE INDEX заново.
--
-- ВНИМАНИЕ: DROP+ADD GENERATED STORED-колонки на непустой таблице запускает
-- полный rewrite — может быть долгим на большом KB. Выполняется в той же
-- транзакции, что и весь apply_bootstrap (см. migrations.py).

DO $$
DECLARE
    expr_text text;
BEGIN
    SELECT pg_get_expr(ad.adbin, ad.adrelid)
      INTO expr_text
      FROM pg_attribute a
      JOIN pg_class     c  ON c.oid  = a.attrelid
      JOIN pg_namespace n  ON n.oid  = c.relnamespace
      LEFT JOIN pg_attrdef ad
             ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
     WHERE a.attname = 'tsv'
       AND c.relname = {chunks_name_lit}
       AND n.nspname = {schema_name_lit};

    -- Уже мигрировано — выходим
    IF expr_text IS NOT NULL AND expr_text LIKE '%''english''%' THEN
        RETURN;
    END IF;

    -- DROP COLUMN автоматически роняет зависимый GIN-индекс
    ALTER TABLE {chunks_table} DROP COLUMN IF EXISTS tsv;

    ALTER TABLE {chunks_table}
        ADD COLUMN tsv tsvector
        GENERATED ALWAYS AS (
            to_tsvector(
                'russian',
                {schema}.immutable_unaccent(coalesce(format_content, ''))
            )
            || to_tsvector(
                'english',
                {schema}.immutable_unaccent(coalesce(format_content, ''))
            )
        ) STORED;

    CREATE INDEX IF NOT EXISTS {chunks_tsv_gin_name}
        ON {chunks_table} USING gin (tsv);
END $$;
