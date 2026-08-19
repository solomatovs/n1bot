create extension if not exists btree_gin;

create index if not exists {chunks_collection_tsv_gin_name}
    on {chunks_table} using gin (collection, tsv);

create index if not exists {chunks_collection_source_chunk_idx_name}
    on {chunks_table} (collection, source_id, chunk_index);

drop index if exists {chunks_tsv_gin_qualified};
drop index if exists {chunks_collection_idx_qualified};
drop index if exists {chunks_collection_source_idx_qualified};
