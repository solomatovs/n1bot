-- @name tables
-- @wave 2
-- @params dbs
-- @max 26.5
select
    database,
    name,
    uuid,
    engine,
    engine_full,
    create_table_query,
    as_select,
    partition_key,
    sorting_key,
    primary_key,
    sampling_key,
    storage_policy,
    total_rows,
    total_bytes,
    comment,
    metadata_modification_time as modified_at,
    dependencies_database,
    dependencies_table,
    loading_dependencies_database,
    loading_dependencies_table,
    cast(null, 'Nullable(String)') as target_database,
    cast(null, 'Nullable(String)') as target_table,
    hex(sipHash64(tuple(
        database, name, uuid, engine, engine_full, create_table_query, as_select,
        partition_key, sorting_key, primary_key, sampling_key, storage_policy, comment,
        dependencies_database, dependencies_table,
        loading_dependencies_database, loading_dependencies_table
    ))) as row_version
from
    system.tables
where
    database in {dbs:Array(String)}
-- @verify
select
    database,
    name,
    hex(sipHash64(tuple(
        database, name, uuid, engine, engine_full, create_table_query, as_select,
        partition_key, sorting_key, primary_key, sampling_key, storage_policy, comment,
        dependencies_database, dependencies_table,
        loading_dependencies_database, loading_dependencies_table
    ))) as row_version
from
    system.tables
where
    database in {dbs:Array(String)}
