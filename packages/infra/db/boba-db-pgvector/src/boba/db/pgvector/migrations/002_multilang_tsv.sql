-- Multilang FTS: пересоздать tsv-колонку как concat русского и английского
-- tsvector'ов. Без этой миграции tsv хранит только russian-стемминг —
-- английские запросы попадают в FTS-канал с потерей recall.
--
-- Идемпотентность: проверяем generated-выражение колонки `tsv` на наличие
-- маркера `'english'`. Если есть — миграция уже применена, выходим.
-- Иначе drop column (зависимый GIN-индекс падает каскадом) + add column
-- с новым выражением + create index заново.
--
-- ВНИМАНИЕ: drop+add generated stored-колонки на непустой таблице запускает
-- полный rewrite — может быть долгим на большом KB. Выполняется в той же
-- транзакции, что и весь apply_bootstrap (см. migrations.py).

do $$
declare
    expr_text text;
begin
    select
        pg_get_expr(ad.adbin, ad.adrelid)
    into
        expr_text
    from
        pg_attribute a
        join pg_class     c on c.oid = a.attrelid
        join pg_namespace n on n.oid = c.relnamespace
        left join pg_attrdef ad
            on ad.adrelid = a.attrelid
            and ad.adnum = a.attnum
    where
        a.attname = 'tsv'
        and c.relname = {chunks_name_lit}
        and n.nspname = {schema_name_lit};

    -- Уже мигрировано — выходим
    if expr_text is not null and expr_text like '%''english''%' then
        return;
    end if;

    -- drop column автоматически роняет зависимый GIN-индекс
    alter table {chunks_table} drop column if exists tsv;

    alter table {chunks_table}
        add column tsv tsvector
        generated always as (
            to_tsvector(
                'russian',
                {schema}.immutable_unaccent(coalesce(format_content, ''))
            )
            || to_tsvector(
                'english',
                {schema}.immutable_unaccent(coalesce(format_content, ''))
            )
        ) stored;

    create index if not exists {chunks_tsv_gin_name}
        on {chunks_table} using gin (tsv);
end $$;
