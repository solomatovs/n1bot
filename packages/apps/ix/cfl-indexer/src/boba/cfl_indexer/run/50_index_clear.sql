/*
cfl-indexer, текст node, шаг 1: снять прежние строки текста этого node из полнотекста.

Индексатор пишет в общий {schema}.ix_fts только то, чего SQL добыть не может: markdown
страницы, текст вложения, OCR. Остальные аспекты выводят из объявлений общие
индексаторы ix-fts, ix-trgm и ix-vector, поэтому снимаются здесь только строки
аспектов, которые кладёт сам индексатор.
*/
-- @name index_clear
-- @params node_id aspects
delete from {schema}.ix_fts
where
    node_id = %(node_id)s
    and aspect::varchar = any(%(aspects)s::varchar[]);
