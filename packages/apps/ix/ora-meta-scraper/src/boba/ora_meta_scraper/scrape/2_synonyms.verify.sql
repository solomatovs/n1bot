select
    s.obj# as obj_id,
    rawtohex(standard_hash(s.obj# || '|' || s.node || '|' || s.owner || '|' || s.name, 'MD5')) as row_version
from
    sys.syn$ s
where
    s.obj# in (select o.obj# from sys.obj$ o where o.owner# in {owners} and o.type# in (5) and {objects})
