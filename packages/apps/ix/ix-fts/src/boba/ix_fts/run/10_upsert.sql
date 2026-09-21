/*
ix-fts, шаг 1: вставить недостающие строки и обновить те, у которых content
изменился, пачкой размером %(batch)s. Повторять, пока applied не станет 0.
Строки берутся в порядке ключа (node_id, surface, aspect): два воркера, обновляющие одни и те
же строки, берут замки в одном порядке и не заходят в deadlock.
Источник аспектов подставляется вместо плейсхолдера sources: воркер собирает его при
старте из объявлений {schema}.surface_aspect по классам из конфига.
Строка сравнивается и по tsv: текст, положенный скрапером напрямую (markdown страницы
Confluence, текст вложения), приходит без весов, и этот шаг выравнивает его по весам
из конфига индексатора.
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
        left join {schema}.ix_fts f
            on  f.node_id = a.node_id
            and f.surface = a.surface
            and f.aspect  = a.aspect
    where
        f.node_id is null
        or f.content is distinct from a.content
        or f.tsv is distinct from a.tsv
    order by
        a.node_id, a.surface, a.aspect
    limit
        %(batch)s
),
done as (
    insert into {schema}.ix_fts
        (node_id, surface, aspect, content, tsv)
    select
        node_id, surface, aspect, content, tsv
    from
        todo
    on conflict (node_id, surface, aspect) do update
        set content = excluded.content,
            tsv     = excluded.tsv
    where
        ix_fts.content is distinct from excluded.content
        or ix_fts.tsv is distinct from excluded.tsv
    returning 1
)
select
    'upsert' as op,
    (select count(*) from todo) as planned,
    (select count(*) from done) as applied;
