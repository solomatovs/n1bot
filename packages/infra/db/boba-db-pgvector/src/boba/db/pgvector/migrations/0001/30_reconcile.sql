select pg_temp.reconcile_tables({schema_name_lit}, array[
    format('pg_temp.%I', {collections_name})::regclass,
    format('pg_temp.%I', {chunks_name})::regclass,
    format('pg_temp.%I', {sources_name})::regclass
]);
