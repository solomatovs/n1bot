/*
cfl-indexer, вектор node, шаг 1: аспекты класса description с md5 текста; сравниваются
с content_hash уже записанных чанков, и модель считает только изменившиеся.
*/
-- @name vector_candidates
-- @params node_id
select
    a.surface::varchar as surface,
    a.aspect::varchar as aspect,
    a.content,
    md5(a.content) as content_hash
from
    ({sources}) a
    join {schema}.aspect x on x.aspect = a.aspect
where
    a.node_id = %(node_id)s
    and x.class = 'description'
order by
    a.aspect;
