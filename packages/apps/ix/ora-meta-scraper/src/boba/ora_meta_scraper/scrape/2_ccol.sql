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
    c.obj# in (select o.obj# from sys.obj$ o where o.owner# in {owners} and o.type# in (2, 4) and {objects})
