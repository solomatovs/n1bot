-- @name comment
-- @params node_id space_key page_id comment_id location version created_at updated_at author content_hash indexer_hash
insert into {schema}.cfl_comment
    (
        node_id, space_key, page_id, comment_id, location, version, created_at,
        updated_at, author, content_hash, indexer_hash
    )
values
    (
        %(node_id)s,
        %(space_key)s,
        %(page_id)s,
        %(comment_id)s,
        %(location)s,
        %(version)s,
        %(created_at)s,
        %(updated_at)s,
        %(author)s,
        %(content_hash)s,
        %(indexer_hash)s
    )
on conflict (node_id) do update
    set space_key    = excluded.space_key,
        page_id      = excluded.page_id,
        comment_id   = excluded.comment_id,
        location     = excluded.location,
        version      = excluded.version,
        created_at   = excluded.created_at,
        updated_at   = excluded.updated_at,
        author       = excluded.author,
        content_hash = excluded.content_hash,
        indexer_hash = excluded.indexer_hash;
