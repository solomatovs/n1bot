select
    c.obj# as obj_id,
    c.intcol# as intcol_id,
    rawtohex(standard_hash(c.obj# || '|' || c.col# || '|' || c.intcol# || '|' || c.segcol# || '|' || c.name || '|' || c.type# || '|' || c.length || '|' || c.precision# || '|' || c.scale || '|' || c.null$ || '|' || c.deflength || '|' || c.property || '|' || c.charsetform || '|' || c.spare3, 'MD5')) as row_version
from
    sys.col$ c
where
    c.obj# in (select o.obj# from sys.obj$ o where o.owner# in {owners} and o.type# in (2, 4) and {objects})
