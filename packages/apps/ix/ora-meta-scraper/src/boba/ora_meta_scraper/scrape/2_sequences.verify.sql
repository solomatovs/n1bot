select
    s.obj# as obj_id,
    rawtohex(standard_hash(s.obj# || '|' || s.increment$ || '|' || s.minvalue || '|' || s.maxvalue || '|' || s.cycle# || '|' || s.order$ || '|' || s.cache || '|' || s.flags, 'MD5')) as row_version
from
    sys.seq$ s
where
    s.obj# in (select o.obj# from sys.obj$ o where o.owner# in {owners} and o.type# in (6) and {objects})
