select
    s.obj# as obj_id,
    s.increment$ as increment_by,
    s.minvalue as min_value,
    s.maxvalue as max_value,
    s.cycle# as cycle_flag,
    s.order$ as order_flag,
    s.cache as cache_size,
    s.flags,
    rawtohex(standard_hash(s.obj# || '|' || s.increment$ || '|' || s.minvalue || '|' || s.maxvalue || '|' || s.cycle# || '|' || s.order$ || '|' || s.cache || '|' || s.flags, 'MD5')) as row_version
from
    sys.seq$ s
where
    s.obj# in (select o.obj# from sys.obj$ o where o.owner# in {owners} and o.type# in (6) and {objects})
