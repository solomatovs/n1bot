-- @name attachment
-- @params node_id space_key page_id attachment_id title media_type file_size version created_at updated_at author content_hash indexer_hash
insert into {schema}.cfl_attachment
    (
        node_id, space_key, page_id, attachment_id, title, media_type, file_size,
        version, created_at, updated_at, author, content_hash, indexer_hash
    )
values
    (
        %(node_id)s,
        %(space_key)s,
        %(page_id)s,
        %(attachment_id)s,
        %(title)s,
        %(media_type)s,
        %(file_size)s,
        %(version)s,
        %(created_at)s,
        %(updated_at)s,
        %(author)s,
        %(content_hash)s,
        %(indexer_hash)s
    )
on conflict (node_id) do update
    set space_key     = excluded.space_key,
        page_id       = excluded.page_id,
        attachment_id = excluded.attachment_id,
        title         = excluded.title,
        media_type    = excluded.media_type,
        file_size     = excluded.file_size,
        version       = excluded.version,
        created_at    = excluded.created_at,
        updated_at    = excluded.updated_at,
        author        = excluded.author,
        content_hash  = excluded.content_hash,
        indexer_hash  = excluded.indexer_hash;
