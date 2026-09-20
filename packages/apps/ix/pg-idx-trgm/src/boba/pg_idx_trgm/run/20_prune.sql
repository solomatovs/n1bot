/*
pg-idx-trgm, шаг 2: удалить строки pg_trgm, для которых аспекта больше нет: node удалён
или сменил поверхность.
Источник аспектов подставляется вместо плейсхолдера sources: воркер собирает его при
старте из объявлений {schema}.surface_aspect по классам из конфига.
*/
-- @name prune
with aspect as (
    {sources}
),
stale as (
    select
        f.node_id, f.surface, f.aspect
    from
        {schema}.pg_idx_trgm f
    where
        not exists (
            select 1
            from   aspect a
            where  a.node_id = f.node_id
              and  a.surface = f.surface
              and  a.aspect  = f.aspect
        )
    order by
        f.node_id, f.surface, f.aspect
    for update skip locked
),
done as (
    delete from {schema}.pg_idx_trgm f
    using
        stale s
    where
        f.node_id = s.node_id
        and f.surface = s.surface
        and f.aspect = s.aspect
    returning 1
)
select
    'prune' as op,
    (select count(*) from done) as deleted;
