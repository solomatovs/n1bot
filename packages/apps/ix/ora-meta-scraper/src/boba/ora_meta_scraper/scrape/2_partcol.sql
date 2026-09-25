select
    p.obj# as obj_id,
    p.intcol# as intcol_id,
    p.pos# as pos,
    rawtohex(standard_hash(p.obj# || '|' || p.intcol# || '|' || p.pos#, 'MD5')) as row_version
from
    sys.partcol$ p
where
    p.obj# in (select o.obj# from sys.obj$ o where o.owner# in (select u.user# from sys.user$ u where u.type# = 1 and bitand(nvl(u.spare1, 0), 256) = 0) and o.type# in (2) and o.subname is null and o.linkname is null and o.remoteowner is null and bitand(o.flags, 128) = 0)
