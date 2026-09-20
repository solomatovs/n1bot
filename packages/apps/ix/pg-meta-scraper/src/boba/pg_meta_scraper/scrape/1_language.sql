-- @name language
-- @wave 1
-- @key oid
select
    oid,
    lanname,
    xmin::text as row_xmin
from
    pg_language;
-- @verify
select
    oid,
    xmin::text as row_xmin
from
    pg_language;
