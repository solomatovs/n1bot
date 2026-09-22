select
    database,
    name,
    hex(sipHash64(tuple(
        database, name, uuid, origin, type, `key.names`, `key.types`,
        `attribute.names`, `attribute.types`, source, lifetime_min, lifetime_max, comment
    ))) as row_version
from
    system.dictionaries
where
    database in {dbs:Array(String)}
