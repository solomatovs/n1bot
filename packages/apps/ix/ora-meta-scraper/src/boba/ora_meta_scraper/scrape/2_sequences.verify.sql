select
    s.obj# as obj_id,
    rawtohex(standard_hash(s.obj# || '|' || s.increment$ || '|' || s.minvalue || '|' || s.maxvalue || '|' || s.cycle# || '|' || s.order$ || '|' || s.cache || '|' || s.flags, 'MD5')) as row_version
from
    sys.seq$ s
where
    s.obj# in (select o.obj# from sys.obj$ o where o.owner# in (select u.user# from sys.user$ u where u.type# = 1 and bitand(nvl(u.spare1, 0), 256) = 0) and o.type# in (6) and o.subname is null and o.linkname is null and o.remoteowner is null and bitand(o.flags, 128) = 0)
