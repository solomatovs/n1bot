-- @name space_state
-- @params node_id
select
    0 as version,
    content_hash,
    indexer_hash
from
    {schema}.cfl_space
where
    node_id = %(node_id)s;
