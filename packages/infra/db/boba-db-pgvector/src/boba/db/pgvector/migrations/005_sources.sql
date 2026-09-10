create table if not exists {sources_table} (
    collection   text not null,
    source_id    text not null,
    parent_id    text not null default '',
    fingerprint  text not null,
    content_hash text not null default '',
    grade        int  not null default 0,
    stamp        text not null,
    seen_at      timestamptz not null,
    indexed_at   timestamptz not null,
    primary key (collection, source_id)
);

create index if not exists {sources_collection_seen_idx_name}
    on {sources_table} (collection, seen_at);

create index if not exists {sources_collection_parent_idx_name}
    on {sources_table} (collection, parent_id);
