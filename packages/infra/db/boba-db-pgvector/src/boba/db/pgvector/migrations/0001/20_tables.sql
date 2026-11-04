create temporary table {collections_table} (
    name text primary key,
    description text not null default '',
    created_at timestamptz not null default now()
);

create temporary table {chunks_table} (
    chunk_id text primary key,
    collection text not null,
    source_id text not null,
    chunk_index integer not null,
    content_hash text not null,
    raw_content text not null,
    format_content text not null,
    embedding vector,
    metadata jsonb not null default '{{}}'::jsonb,
    tags text[] not null default '{{}}',
    updated_at timestamptz not null default now(),
    tsv tsvector generated always as (
        setweight(to_tsvector('russian', {schema}.immutable_unaccent(coalesce(metadata->>'reader.page_title', ''))), 'A')
        || setweight(to_tsvector('english', {schema}.immutable_unaccent(coalesce(metadata->>'reader.page_title', ''))), 'A')
        || setweight(to_tsvector('russian', {schema}.immutable_unaccent(coalesce(metadata->>'section.heading.path', ''))), 'B')
        || setweight(to_tsvector('english', {schema}.immutable_unaccent(coalesce(metadata->>'section.heading.path', ''))), 'B')
        || setweight(to_tsvector('russian', {schema}.immutable_unaccent(coalesce(format_content, ''))), 'D')
        || setweight(to_tsvector('english', {schema}.immutable_unaccent(coalesce(format_content, ''))), 'D')
    ) stored
);

create index {chunks_search_index} on {chunks_table} using gin (collection, tsv);
create index {chunks_source_index} on {chunks_table} (collection, source_id, chunk_index);
create index {chunks_vector_index} on {chunks_table} using hnsw ((embedding::vector({embedding_dim})) vector_cosine_ops);

create temporary table {sources_table} (
    collection text not null,
    source_id text not null,
    parent_id text not null default '',
    fingerprint text not null,
    content_hash text not null default '',
    grade integer not null default 0,
    stamp text not null,
    seen_at timestamptz not null,
    indexed_at timestamptz not null,
    seen_run text not null default '',
    scope text not null default '',
    primary key (collection, source_id)
);

create index {sources_seen_index} on {sources_table} (collection, seen_at);
create index {sources_parent_index} on {sources_table} (collection, parent_id);
create index {sources_scope_index} on {sources_table} (collection, scope, seen_run);
create index {sources_run_index} on {sources_table} (collection, parent_id, seen_run);
