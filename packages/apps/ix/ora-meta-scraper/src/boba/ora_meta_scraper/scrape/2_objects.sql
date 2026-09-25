select
    o.obj# as obj_id,
    o.owner# as owner_id,
    o.name,
    o.namespace,
    o.type# as type_id,
    o.ctime as created,
    o.mtime as last_ddl_time,
    o.stime as spec_time,
    o.status,
    o.flags,
    rawtohex(standard_hash(o.obj# || '|' || o.owner# || '|' || o.name || '|' || o.namespace || '|' || o.type# || '|' || to_char(o.ctime, 'YYYYMMDDHH24MISS') || '|' || to_char(o.mtime, 'YYYYMMDDHH24MISS') || '|' || to_char(o.stime, 'YYYYMMDDHH24MISS') || '|' || o.status || '|' || o.flags, 'MD5')) as row_version
from
    sys.obj$ o
where
    o.owner# in (select u.user# from sys.user$ u where u.type# = 1 and bitand(nvl(u.spare1, 0), 256) = 0)
    and o.type# in (1, 2, 4, 5, 6, 7, 8, 9, 12, 13, 42)
    and o.subname is null and o.linkname is null and o.remoteowner is null and bitand(o.flags, 128) = 0
