-- @name synonyms
-- @wave 2
select
    s.obj# as obj_id,
    s.node,
    s.owner as owner_name,
    s.name,
    rawtohex(standard_hash(s.obj# || '|' || s.node || '|' || s.owner || '|' || s.name, 'MD5')) as row_version
from
    sys.syn$ s
where
    s.obj# in (select o.obj# from sys.obj$ o where o.owner# in (select u.user# from sys.user$ u where u.type# = 1 and bitand(nvl(u.spare1, 0), 256) = 0) and o.type# in (5) and o.subname is null and o.linkname is null and o.remoteowner is null and bitand(o.flags, 128) = 0)
-- @verify
select
    s.obj# as obj_id,
    rawtohex(standard_hash(s.obj# || '|' || s.node || '|' || s.owner || '|' || s.name, 'MD5')) as row_version
from
    sys.syn$ s
where
    s.obj# in (select o.obj# from sys.obj$ o where o.owner# in (select u.user# from sys.user$ u where u.type# = 1 and bitand(nvl(u.spare1, 0), 256) = 0) and o.type# in (5) and o.subname is null and o.linkname is null and o.remoteowner is null and bitand(o.flags, 128) = 0)
