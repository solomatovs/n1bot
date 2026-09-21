-- @name attachment_state
-- @params node_id
select
    version,
    content_hash,
    indexer_hash
from
    {schema}.cfl_attachment
where
    node_id = %(node_id)s;
