/*
cfl-indexer, индекс node,
шаг 1: снять строки триграмм и полнотекста node перед перезаписью.
Чанки эмбеддингов не трогаются
их сверяет по content_hash шаг векторизации
*/
-- @name index_clear
-- @params node_id
with trgm as (
    delete from {schema}.cfl_idx_trgm
    where
        node_id = %(node_id)s
    returning 1
),
fts as (
    delete from {schema}.cfl_idx_fts
    where
        node_id = %(node_id)s
    returning 1
)
select
    (select count(*) from trgm) as trgm,
    (select count(*) from fts) as fts;
