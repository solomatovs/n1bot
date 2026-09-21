/*
cfl-indexer, индекс node, шаг 2: текст, который SQL добыть не может (markdown страницы,
текст вложения, OCR), кладётся в полнотекст напрямую; объявления body и ocr читают его
отсюда.
*/
-- @name index_push
-- @params node_id surface aspect content weight
insert into {schema}.cfl_idx_fts
    (node_id, surface, aspect, content, tsv)
values
    (
        %(node_id)s,
        %(surface)s::{schema}.surface_e,
        %(aspect)s::{schema}.aspect_e,
        %(content)s::varchar,
        setweight(to_tsvector('russian', %(content)s::varchar), %(weight)s::"char")
    )
on conflict (node_id, surface, aspect) do update
    set content = excluded.content,
        tsv     = excluded.tsv;
