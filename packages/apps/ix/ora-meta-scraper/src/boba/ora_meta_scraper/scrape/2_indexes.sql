select
    i.obj# as obj_id,
    i.bo# as bo_id,
    i.ts# as ts_id,
    i.type# as type_id,
    i.property,
    i.flags,
    i.intcols,
    i.spare2 as prefix_len,
    rawtohex(standard_hash(i.obj# || '|' || i.bo# || '|' || i.ts# || '|' || i.type# || '|' || i.property || '|' || i.flags || '|' || i.intcols || '|' || i.spare2, 'MD5')) as row_version
from
    sys.ind$ i
where
    i.obj# in (select o.obj# from sys.obj$ o where o.owner# in {owners} and o.type# in (1) and {objects})
