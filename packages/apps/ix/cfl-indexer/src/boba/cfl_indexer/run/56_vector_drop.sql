/*
cfl-indexer, вектор node, шаг 3: снять чанки аспектов, которых у node больше нет.
*/
-- @name vector_drop
-- @params node_id aspects
delete from {schema}.cfl_idx_emb_e5_1024
where
    node_id = %(node_id)s
    and aspect::varchar <> all(%(aspects)s::varchar[]);
