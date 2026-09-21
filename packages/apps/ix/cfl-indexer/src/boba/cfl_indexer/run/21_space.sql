-- @name space
-- @params node_id space_key name space_type status description content_hash indexer_hash
insert into {schema}.cfl_space
    (node_id, space_key, name, space_type, status, description, content_hash, indexer_hash)
values
    (
        %(node_id)s,
        %(space_key)s,
        %(name)s,
        %(space_type)s,
        %(status)s,
        %(description)s,
        %(content_hash)s,
        %(indexer_hash)s
    )
on conflict (node_id) do update
    set space_key    = excluded.space_key,
        name         = excluded.name,
        space_type   = excluded.space_type,
        status       = excluded.status,
        description  = excluded.description,
        content_hash = excluded.content_hash,
        indexer_hash = excluded.indexer_hash;
