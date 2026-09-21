/*
cfl-indexer: тексты node, положенные в полнотекст из Python. Нужны, когда версия
вложения сменилась, а байты файла нет: строка переписывается с прежним текстом без
повторного разбора и OCR.
*/
-- @name pushed_texts
-- @params node_id
select
    aspect::varchar as aspect,
    content
from
    {schema}.ix_fts
where
    node_id = %(node_id)s
    and aspect in ('body', 'ocr')
order by
    aspect;
