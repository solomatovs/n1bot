/*
pg-llm-describer, шаг 3: удалить описания объектов, для которых больше нет входа: node удалён
или поверхность перестала отдавать аспект класса describer_input. Занятые другим сеансом
строки пропускаются.
*/
-- @name prune
with input as (
    {sources}
),
stale as (
    select
        s.node_id
    from
        {schema}.pg_llm_description s
    where
        not exists (
            select 1
            from   input i
            where  i.node_id = s.node_id
        )
    order by
        s.node_id
    for update skip locked
),
done as (
    delete from {schema}.pg_llm_description s
    using
        stale x
    where
        s.node_id = x.node_id
    returning 1
)
select
    'prune' as op,
    (select count(*) from done) as deleted;
