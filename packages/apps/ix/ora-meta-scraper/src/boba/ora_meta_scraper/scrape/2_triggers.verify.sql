select
    t.obj# as obj_id,
    rawtohex(standard_hash(t.obj# || '|' || t.baseobject || '|' || t.type# || '|' || t.insert$ || '|' || t.update$ || '|' || t.delete$ || '|' || t.enabled || '|' || t.property, 'MD5')) as row_version
from
    sys.trigger$ t
where
    t.obj# in (select o.obj# from sys.obj$ o where o.owner# in (select u.user# from sys.user$ u where u.type# = 1 and bitand(nvl(u.spare1, 0), 256) = 0) and o.type# in (12) and o.subname is null and o.linkname is null and o.remoteowner is null and bitand(o.flags, 128) = 0)
