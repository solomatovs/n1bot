select
    t.obj# as obj_id,
    rawtohex(standard_hash(t.obj# || '|' || t.baseobject || '|' || t.type# || '|' || t.insert$ || '|' || t.update$ || '|' || t.delete$ || '|' || t.enabled || '|' || t.property, 'MD5')) as row_version
from
    sys.trigger$ t
where
    t.obj# in (select o.obj# from sys.obj$ o where o.owner# in {owners} and o.type# in (12) and {objects})
