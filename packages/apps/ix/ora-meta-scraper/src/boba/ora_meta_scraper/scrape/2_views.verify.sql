select
    v.obj# as obj_id,
    rawtohex(standard_hash(v.obj# || '|' || v.textlength || '|' || v.property, 'MD5')) as row_version
from
    sys.view$ v
where
    v.obj# in (select o.obj# from sys.obj$ o where o.owner# in {owners} and o.type# in (4) and {objects})
