select
    database,
    name,
    hex(sipHash64(tuple(
        database, name, uuid, engine, engine_full, create_table_query, as_select,
        partition_key, sorting_key, primary_key, sampling_key, storage_policy, comment,
        dependencies_database, dependencies_table,
        loading_dependencies_database, loading_dependencies_table,
        target_database, target_table
    ))) as row_version
from
    system.tables
where
    database in {dbs:Array(String)}
