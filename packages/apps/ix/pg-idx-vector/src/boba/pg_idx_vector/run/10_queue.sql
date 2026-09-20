/*
pg-idx-vector, шаг 1: очередь на расчёт. Аспекты, у которых нет ни одного чанка или
content_hash чанков отличается от md5 полного текста, в порядке ключа, пачкой %(batch)s. Каждая
выданная строка захвачена сессионным advisory-замком (ключ: хэш 'pg_emb' и node_id),
чтобы второй воркер не считал её одновременно; замки снимает 90_unlock.sql после записи
или обрыв сессии. Строки, занятые другим воркером, пропускаются.
Источник аспектов подставляется вместо плейсхолдера sources: воркер собирает его при
старте из объявлений {schema}.surface_aspect по классам из конфига.
*/
-- @name queue
-- @params batch
with aspect as (
    {sources}
),
todo as (
    select
        a.node_id,
        a.surface,
        a.aspect,
        a.content,
        md5(a.content) as content_hash
    from
        aspect a
        left join (
            select distinct node_id, surface, aspect, content_hash
            from   {schema}.pg_idx_emb_e5_1024
        ) e
            on  e.node_id = a.node_id
            and e.surface = a.surface
            and e.aspect  = a.aspect
    where
        e.node_id is null
        or e.content_hash <> md5(a.content)
    order by
        a.node_id, a.surface, a.aspect
    limit
        %(batch)s * 4
)
select
    node_id, surface, aspect, content, content_hash
from
    todo
where
    pg_try_advisory_lock(hashtextextended('pg_emb', node_id))
limit
    %(batch)s;
