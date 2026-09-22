-- @name views
-- @wave 2
select
    v.obj# as obj_id,
    v.textlength,
    v.text,
    v.property,
    rawtohex(standard_hash(v.obj# || '|' || v.textlength || '|' || v.property, 'MD5')) as row_version
from
    sys.view$ v
where
    v.obj# in (select o.obj# from sys.obj$ o where o.owner# in (select u.user# from sys.user$ u where u.type# = 1 and bitand(nvl(u.spare1, 0), 256) = 0) and o.type# in (4) and o.subname is null and o.linkname is null and o.remoteowner is null and bitand(o.flags, 128) = 0)
-- @verify
select
    v.obj# as obj_id,
    rawtohex(standard_hash(v.obj# || '|' || v.textlength || '|' || v.property, 'MD5')) as row_version
from
    sys.view$ v
where
    v.obj# in (select o.obj# from sys.obj$ o where o.owner# in (select u.user# from sys.user$ u where u.type# = 1 and bitand(nvl(u.spare1, 0), 256) = 0) and o.type# in (4) and o.subname is null and o.linkname is null and o.remoteowner is null and bitand(o.flags, 128) = 0)
