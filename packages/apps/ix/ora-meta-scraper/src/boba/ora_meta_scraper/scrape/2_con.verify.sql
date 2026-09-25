select
    c.con# as con_id,
    rawtohex(standard_hash(c.con# || '|' || c.owner# || '|' || c.name, 'MD5')) as row_version
from
    sys.con$ c
where
    c.owner# in (select u.user# from sys.user$ u where u.type# = 1 and bitand(nvl(u.spare1, 0), 256) = 0)
