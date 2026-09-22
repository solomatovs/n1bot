select
    database,
    name,
    uuid,
    origin,
    type as layout,
    toJSONString(`key.names`) as key_names,
    toJSONString(`key.types`) as key_types,
    toJSONString(`attribute.names`) as attribute_names,
    toJSONString(`attribute.types`) as attribute_types,
    source,
    lifetime_min,
    lifetime_max,
    comment,
    hex(sipHash64(tuple(
        database, name, uuid, origin, type, `key.names`, `key.types`,
        `attribute.names`, `attribute.types`, source, lifetime_min, lifetime_max, comment
    ))) as row_version
from
    system.dictionaries
where
    database in {dbs:Array(String)}
