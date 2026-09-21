-- @name vector_state
-- @params node_id
select distinct
    aspect::varchar as aspect,
    content_hash
from
    {schema}.cfl_idx_emb_e5_1024
where
    node_id = %(node_id)s;
