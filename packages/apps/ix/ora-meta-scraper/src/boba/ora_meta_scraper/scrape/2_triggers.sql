select
    t.obj# as obj_id,
    t.baseobject as base_obj_id,
    t.type# as type_id,
    t.insert$ as insert_flag,
    t.update$ as update_flag,
    t.delete$ as delete_flag,
    t.enabled,
    t.property,
    rawtohex(standard_hash(t.obj# || '|' || t.baseobject || '|' || t.type# || '|' || t.insert$ || '|' || t.update$ || '|' || t.delete$ || '|' || t.enabled || '|' || t.property, 'MD5')) as row_version
from
    sys.trigger$ t
where
    t.obj# in (select o.obj# from sys.obj$ o where o.owner# in {owners} and o.type# in (12) and {objects})
