-- @name columns
-- @wave 2
-- @params dbs
select
    database,
    table as table_name,
    name,
    type,
    position,
    default_kind,
    default_expression,
    comment,
    is_in_partition_key,
    is_in_sorting_key,
    is_in_primary_key,
    is_in_sampling_key,
    compression_codec,
    hex(sipHash64(tuple(
        database, table, name, type, position, default_kind, default_expression, comment,
        is_in_partition_key, is_in_sorting_key, is_in_primary_key, is_in_sampling_key,
        compression_codec
    ))) as row_version
from
    system.columns
where
    database in {dbs:Array(String)}
-- @verify
select
    database,
    table as table_name,
    name,
    hex(sipHash64(tuple(
        database, table, name, type, position, default_kind, default_expression, comment,
        is_in_partition_key, is_in_sorting_key, is_in_primary_key, is_in_sampling_key,
        compression_codec
    ))) as row_version
from
    system.columns
where
    database in {dbs:Array(String)}
