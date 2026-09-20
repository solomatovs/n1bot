/*
pg-idx-vector, шаг 3: удалить чанки, для которых аспекта больше нет: node удалён,
поверхность сменилась, комментарий или колонки пропали. Строки, занятые другим сеансом,
пропускаются через for update skip locked и уходят при следующем prune.
Источник аспектов подставляется вместо плейсхолдера sources: воркер собирает его при
старте из объявлений {schema}.surface_aspect по классам из конфига.
*/
-- @name prune
with aspect as (
    {sources}
),
stale as (
    select
        e.node_id, e.surface, e.aspect
    from
        {schema}.pg_idx_emb_e5_1024 e
    where
        not exists (
            select 1
            from   aspect a
            where  a.node_id = e.node_id
              and  a.surface = e.surface
              and  a.aspect  = e.aspect
        )
    order by
        e.node_id, e.surface, e.aspect
    for update skip locked
),
done as (
    delete from {schema}.pg_idx_emb_e5_1024 e
    using
        stale s
    where
        e.node_id = s.node_id
        and e.surface = s.surface
        and e.aspect = s.aspect
    returning 1
)
select
    'prune' as op,
    (select count(*) from done) as deleted;
