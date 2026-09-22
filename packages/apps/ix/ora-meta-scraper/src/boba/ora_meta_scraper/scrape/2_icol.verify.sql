select
    i.obj# as obj_id,
    i.pos# as pos,
    rawtohex(standard_hash(i.obj# || '|' || i.bo# || '|' || i.col# || '|' || i.pos# || '|' || i.intcol# || '|' || i.spare1 || '|' || i.spare2, 'MD5')) as row_version
from
    sys.icol$ i
where
    i.obj# in (select o.obj# from sys.obj$ o where o.owner# in {owners} and o.type# in (1) and {objects})
