-- @name blogpost
-- @params node_id space_key content_id title status version created_at updated_at author last_editor labels content_hash indexer_hash
insert into {schema}.cfl_blogpost
    (
        node_id, space_key, content_id, title, status, version, created_at, updated_at,
        author, last_editor, labels, content_hash, indexer_hash
    )
values
    (
        %(node_id)s,
        %(space_key)s,
        %(content_id)s,
        %(title)s,
        %(status)s,
        %(version)s,
        %(created_at)s,
        %(updated_at)s,
        %(author)s,
        %(last_editor)s,
        %(labels)s::varchar[],
        %(content_hash)s,
        %(indexer_hash)s
    )
on conflict (node_id) do update
    set space_key    = excluded.space_key,
        content_id   = excluded.content_id,
        title        = excluded.title,
        status       = excluded.status,
        version      = excluded.version,
        created_at   = excluded.created_at,
        updated_at   = excluded.updated_at,
        author       = excluded.author,
        last_editor  = excluded.last_editor,
        labels       = excluded.labels,
        content_hash = excluded.content_hash,
        indexer_hash = excluded.indexer_hash;
