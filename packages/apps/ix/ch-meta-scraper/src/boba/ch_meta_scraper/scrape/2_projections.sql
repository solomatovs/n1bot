-- @name projections
-- @wave 2
-- @params dbs
-- @min 24.4
select
    database,
    table as table_name,
    name,
    type,
    sorting_key,
    query,
    hex(sipHash64(tuple(database, table, name, type, sorting_key, query))) as row_version
from
    system.projections
where
    database in {dbs:Array(String)}
-- @verify
select
    database,
    table as table_name,
    name,
    hex(sipHash64(tuple(database, table, name, type, sorting_key, query))) as row_version
from
    system.projections
where
    database in {dbs:Array(String)}
