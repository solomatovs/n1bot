-- Weighted FTS: пересоздать tsv-колонку с весами A/B/D.
--
--   A — reader.page_title          (заголовок страницы -> максимальный вес)
--   B — section.heading.path       (иерархия заголовков раздела)
--   D — format_content             (тело чанка -> базовый вес)
--
-- Без этой миграции tsv покрывает только format_content — попадание в
-- название страницы или родительский заголовок ранжируется как обычный
-- body-hit. С weighted-tsv ts_rank_cd(tsv, q) автоматически даёт более
-- высокий score за match в title/heading.
--
-- Идемпотентность: проверяем generated-выражение колонки `tsv` на
-- маркер `'setweight'`. Если есть — миграция уже применена, выходим.
-- Иначе drop column (зависимый GIN-индекс падает каскадом) + add column
-- с новым выражением + create index заново.
--
-- ВНИМАНИЕ: drop+add stored-колонки на непустой таблице запускает
-- полный rewrite — может быть долгим на большом KB.

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
    if expr_text is not null and expr_text like '%setweight%' then
        return;
    end if;

    -- drop column автоматически роняет зависимый GIN-индекс
    alter table {chunks_table} drop column if exists tsv;

    alter table {chunks_table}
        add column tsv tsvector
        generated always as (
            setweight(
                to_tsvector(
                    'russian',
                    {schema}.immutable_unaccent(
                        coalesce(metadata->>'reader.page_title', '')
                    )
                ), 'A'
            )
            || setweight(
                to_tsvector(
                    'english',
                    {schema}.immutable_unaccent(
                        coalesce(metadata->>'reader.page_title', '')
                    )
                ), 'A'
            )
            || setweight(
                to_tsvector(
                    'russian',
                    {schema}.immutable_unaccent(
                        coalesce(metadata->>'section.heading.path', '')
                    )
                ), 'B'
            )
            || setweight(
                to_tsvector(
                    'english',
                    {schema}.immutable_unaccent(
                        coalesce(metadata->>'section.heading.path', '')
                    )
                ), 'B'
            )
            || setweight(
                to_tsvector(
                    'russian',
                    {schema}.immutable_unaccent(coalesce(format_content, ''))
                ), 'D'
            )
            || setweight(
                to_tsvector(
                    'english',
                    {schema}.immutable_unaccent(coalesce(format_content, ''))
                ), 'D'
            )
        ) stored;

    create index if not exists {chunks_tsv_gin_name}
        on {chunks_table} using gin (tsv);
end $$;
