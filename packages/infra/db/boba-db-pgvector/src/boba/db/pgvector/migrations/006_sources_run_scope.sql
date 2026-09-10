alter table {sources_table}
    add column if not exists seen_run text not null default '';

alter table {sources_table}
    add column if not exists scope text not null default '';

create index if not exists {sources_collection_scope_idx_name}
    on {sources_table} (collection, scope, seen_run);

create index if not exists {sources_collection_parent_run_idx_name}
    on {sources_table} (collection, parent_id, seen_run);
