select
    t.obj# as obj_id,
    t.ts# as ts_id,
    t.property,
    t.flags,
    t.trigflag,
    t.rowcnt as row_count,
    rawtohex(standard_hash(t.obj# || '|' || t.ts# || '|' || t.property || '|' || t.flags || '|' || t.trigflag, 'MD5')) as row_version
from
    sys.tab$ t
where
    t.obj# in (select o.obj# from sys.obj$ o where o.owner# in {owners} and o.type# in (2) and {objects})
