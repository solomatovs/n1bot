select
    c.obj# as obj_id,
    c.col# as col_id,
    c.intcol# as intcol_id,
    c.segcol# as segcol_id,
    c.name,
    c.type# as type_id,
    c.length,
    c.precision# as precision_num,
    c.scale,
    c.null$ as null_flag,
    c.deflength,
    c.default$ as default_text,
    c.property,
    c.charsetform,
    c.spare3 as char_length,
    rawtohex(standard_hash(c.obj# || '|' || c.col# || '|' || c.intcol# || '|' || c.segcol# || '|' || c.name || '|' || c.type# || '|' || c.length || '|' || c.precision# || '|' || c.scale || '|' || c.null$ || '|' || c.deflength || '|' || c.property || '|' || c.charsetform || '|' || c.spare3, 'MD5')) as row_version
from
    sys.col$ c
where
    c.obj# in (select o.obj# from sys.obj$ o where o.owner# in (select u.user# from sys.user$ u where u.type# = 1 and bitand(nvl(u.spare1, 0), 256) = 0) and o.type# in (2, 4) and o.subname is null and o.linkname is null and o.remoteowner is null and bitand(o.flags, 128) = 0)
