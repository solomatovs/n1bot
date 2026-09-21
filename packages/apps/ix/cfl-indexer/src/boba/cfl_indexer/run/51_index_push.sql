/*
cfl-indexer, текст node, шаг 2: положить текст, добытый в Python, в общий полнотекст.
Объявления body и ocr читают его отсюда, а вес tsv выравнивает индексатор ix-fts своим
проходом: веса живут в его конфиге, а не здесь.
*/
-- @name index_push
-- @params node_id surface aspect content
insert into {schema}.ix_fts
    (node_id, surface, aspect, content, tsv)
values
    (
        %(node_id)s,
        %(surface)s::{schema}.surface_e,
        %(aspect)s::{schema}.aspect_e,
        %(content)s::varchar,
        to_tsvector('russian', %(content)s::varchar)
    )
on conflict (node_id, surface, aspect) do update
    set content = excluded.content,
        tsv     = excluded.tsv
where
    ix_fts.content is distinct from excluded.content;
