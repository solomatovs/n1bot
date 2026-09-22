select
    c.con# as con_id,
    rawtohex(standard_hash(c.con# || '|' || c.obj# || '|' || c.cols || '|' || c.type# || '|' || c.robj# || '|' || c.rcon# || '|' || c.enabled || '|' || c.defer || '|' || c.refact || '|' || to_char(c.mtime, 'YYYYMMDDHH24MISS'), 'MD5')) as row_version
from
    sys.cdef$ c
where
    c.obj# in (select o.obj# from sys.obj$ o where o.owner# in {owners} and o.type# in (2, 4) and {objects})
