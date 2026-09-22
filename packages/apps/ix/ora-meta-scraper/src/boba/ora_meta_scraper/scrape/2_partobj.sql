select
    p.obj# as obj_id,
    p.parttype,
    p.partcnt,
    p.partkeycols,
    p.spare2,
    rawtohex(standard_hash(p.obj# || '|' || p.parttype || '|' || p.partkeycols || '|' || p.spare2, 'MD5')) as row_version
from
    sys.partobj$ p
where
    p.obj# in (select o.obj# from sys.obj$ o where o.owner# in {owners} and o.type# in (2) and {objects})
