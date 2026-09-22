select
    p.obj# as obj_id,
    p.pos# as pos,
    rawtohex(standard_hash(p.obj# || '|' || p.intcol# || '|' || p.pos#, 'MD5')) as row_version
from
    sys.partcol$ p
where
    p.obj# in (select o.obj# from sys.obj$ o where o.owner# in {owners} and o.type# in (2) and {objects})
