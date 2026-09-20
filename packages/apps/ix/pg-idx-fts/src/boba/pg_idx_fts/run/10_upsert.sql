/*
pg-idx-fts, шаг 1: вставить недостающие строки и обновить те, у которых content
изменился, пачкой размером %(batch)s. Повторять, пока applied не станет 0.
Строки берутся в порядке ключа (node_id, surface, aspect): два воркера, обновляющие одни и те
же строки, берут замки в одном порядке и не заходят в deadlock.
Источник аспектов подставляется вместо плейсхолдера sources: воркер собирает его при
старте из объявлений {schema}.surface_aspect по классам из конфига.
*/
-- @name upsert
-- @params batch
with aspect as (
    select
        a.node_id,
        a.surface,
        a.aspect,
        a.content,
        setweight(
            to_tsvector('russian', a.content), coalesce(w.weight, 'D')::"char"
        ) as tsv
    from
        ({sources}) a
        left join ({weights}) as w(aspect, weight) on w.aspect = a.aspect
),
todo as (
    select
        a.*
    from
        aspect a
        left join {schema}.pg_idx_fts f
            on  f.node_id = a.node_id
            and f.surface = a.surface
            and f.aspect  = a.aspect
    where
        f.node_id is null
        or f.content is distinct from a.content
    order by
        a.node_id, a.surface, a.aspect
    limit
        %(batch)s
),
done as (
    insert into {schema}.pg_idx_fts
        (node_id, surface, aspect, content, tsv)
    select
        node_id, surface, aspect, content, tsv
    from
        todo
    on conflict (node_id, surface, aspect) do update
        set content = excluded.content,
            tsv     = excluded.tsv
    where
        pg_idx_fts.content is distinct from excluded.content
    returning 1
)
select
    'upsert' as op,
    (select count(*) from todo) as planned,
    (select count(*) from done) as applied;
