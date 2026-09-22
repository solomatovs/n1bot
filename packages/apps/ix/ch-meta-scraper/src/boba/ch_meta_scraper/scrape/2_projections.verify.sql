select
    database,
    table as table_name,
    name,
    hex(sipHash64(tuple(database, table, name, type, sorting_key, query))) as row_version
from
    system.projections
where
    database in {dbs:Array(String)}
