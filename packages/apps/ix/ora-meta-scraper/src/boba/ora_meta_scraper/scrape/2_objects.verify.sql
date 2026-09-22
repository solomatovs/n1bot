select
    o.obj# as obj_id,
    rawtohex(standard_hash(o.obj# || '|' || o.owner# || '|' || o.name || '|' || o.namespace || '|' || o.type# || '|' || to_char(o.ctime, 'YYYYMMDDHH24MISS') || '|' || to_char(o.mtime, 'YYYYMMDDHH24MISS') || '|' || to_char(o.stime, 'YYYYMMDDHH24MISS') || '|' || o.status || '|' || o.flags, 'MD5')) as row_version
from
    sys.obj$ o
where
    o.owner# in {owners}
    and o.type# in (1, 2, 4, 5, 6, 7, 8, 9, 12, 13, 42)
    and {objects}
