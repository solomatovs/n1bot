-- @name users
-- @wave 1
select
    u.user# as user_id,
    u.name,
    u.ctime as created,
    rawtohex(standard_hash(u.user# || '|' || u.name || '|' || to_char(u.ctime, 'YYYYMMDDHH24MISS'), 'MD5')) as row_version
from
    sys.user$ u
where
    u.type# = 1 and bitand(nvl(u.spare1, 0), 256) = 0
-- @verify
select
    u.user# as user_id,
    rawtohex(standard_hash(u.user# || '|' || u.name || '|' || to_char(u.ctime, 'YYYYMMDDHH24MISS'), 'MD5')) as row_version
from
    sys.user$ u
where
    u.type# = 1 and bitand(nvl(u.spare1, 0), 256) = 0
