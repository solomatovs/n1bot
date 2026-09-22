-- @name cdef
-- @wave 2
select
    c.con# as con_id,
    c.obj# as obj_id,
    c.cols,
    c.type# as type_id,
    c.robj# as robj_id,
    c.rcon# as rcon_id,
    c.enabled,
    c.defer as defer_flags,
    c.refact,
    c.mtime,
    c.condition,
    rawtohex(standard_hash(c.con# || '|' || c.obj# || '|' || c.cols || '|' || c.type# || '|' || c.robj# || '|' || c.rcon# || '|' || c.enabled || '|' || c.defer || '|' || c.refact || '|' || to_char(c.mtime, 'YYYYMMDDHH24MISS'), 'MD5')) as row_version
from
    sys.cdef$ c
where
    c.obj# in (select o.obj# from sys.obj$ o where o.owner# in (select u.user# from sys.user$ u where u.type# = 1 and bitand(nvl(u.spare1, 0), 256) = 0) and o.type# in (2, 4) and o.subname is null and o.linkname is null and o.remoteowner is null and bitand(o.flags, 128) = 0)
-- @verify
select
    c.con# as con_id,
    rawtohex(standard_hash(c.con# || '|' || c.obj# || '|' || c.cols || '|' || c.type# || '|' || c.robj# || '|' || c.rcon# || '|' || c.enabled || '|' || c.defer || '|' || c.refact || '|' || to_char(c.mtime, 'YYYYMMDDHH24MISS'), 'MD5')) as row_version
from
    sys.cdef$ c
where
    c.obj# in (select o.obj# from sys.obj$ o where o.owner# in (select u.user# from sys.user$ u where u.type# = 1 and bitand(nvl(u.spare1, 0), 256) = 0) and o.type# in (2, 4) and o.subname is null and o.linkname is null and o.remoteowner is null and bitand(o.flags, 128) = 0)
