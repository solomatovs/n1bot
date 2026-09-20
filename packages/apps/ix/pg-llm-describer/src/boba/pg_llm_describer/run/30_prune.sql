/*
pg-llm-describer, шаг 3: удалить описания объектов, которых больше нет или которые перестали
быть таблицей или view. Занятые другим сеансом строки пропускаются.
*/
-- @name prune
with stale as (
    select s.node_id
    from {schema}.pg_llm_description s
    where not exists (select 1 from {schema}.node n where n.id = s.node_id and n.surface in ('pg_meta_table', 'pg_meta_view'))
    order by s.node_id
    for update skip locked
),
done as (
    delete from {schema}.pg_llm_description s using stale x where s.node_id = x.node_id returning 1
)
select 'prune' as op, (select count(*) from done) as deleted;
