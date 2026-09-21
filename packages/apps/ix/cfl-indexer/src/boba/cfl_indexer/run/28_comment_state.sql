-- @name comment_state
-- @params node_id
select
    version,
    content_hash,
    indexer_hash
from
    {schema}.cfl_comment
where
    node_id = %(node_id)s;
