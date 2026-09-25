select
    i.obj# as obj_id,
    i.bo# as bo_id,
    i.col# as col_id,
    i.pos# as pos,
    i.intcol# as intcol_id,
    i.spare1,
    i.spare2,
    rawtohex(standard_hash(i.obj# || '|' || i.bo# || '|' || i.col# || '|' || i.pos# || '|' || i.intcol# || '|' || i.spare1 || '|' || i.spare2, 'MD5')) as row_version
from
    sys.icol$ i
where
    i.obj# in (select o.obj# from sys.obj$ o where o.owner# in (select u.user# from sys.user$ u where u.type# = 1 and bitand(nvl(u.spare1, 0), 256) = 0) and o.type# in (1) and o.subname is null and o.linkname is null and o.remoteowner is null and bitand(o.flags, 128) = 0)
