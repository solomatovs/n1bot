select
    c.con# as con_id,
    c.obj# as obj_id,
    c.col# as col_id,
    c.intcol# as intcol_id,
    c.pos# as pos,
    rawtohex(standard_hash(c.con# || '|' || c.obj# || '|' || c.col# || '|' || c.intcol# || '|' || c.pos#, 'MD5')) as row_version
from
    sys.ccol$ c
where
    c.obj# in (select o.obj# from sys.obj$ o where o.owner# in (select u.user# from sys.user$ u where u.type# = 1 and bitand(nvl(u.spare1, 0), 256) = 0) and o.type# in (2, 4) and o.subname is null and o.linkname is null and o.remoteowner is null and bitand(o.flags, 128) = 0)
