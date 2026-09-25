select
    c.obj# as obj_id,
    c.col# as col_id,
    c.comment$ as comment_text,
    rawtohex(standard_hash(c.obj# || '|' || c.col# || '|' || c.comment$, 'MD5')) as row_version
from
    sys.com$ c
where
    c.obj# in (select o.obj# from sys.obj$ o where o.owner# in (select u.user# from sys.user$ u where u.type# = 1 and bitand(nvl(u.spare1, 0), 256) = 0) and o.type# in (2, 4, 42) and o.subname is null and o.linkname is null and o.remoteowner is null and bitand(o.flags, 128) = 0)
