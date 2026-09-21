-- @name indices
-- @wave 2
-- @params dbs
select
    database,
    table as table_name,
    name,
    type,
    type_full,
    expr,
    granularity,
    hex(sipHash64(tuple(database, table, name, type, type_full, expr, granularity))) as row_version
from
    system.data_skipping_indices
where
    database in {dbs:Array(String)}
-- @verify
select
    database,
    table as table_name,
    name,
    hex(sipHash64(tuple(database, table, name, type, type_full, expr, granularity))) as row_version
from
    system.data_skipping_indices
where
    database in {dbs:Array(String)}
