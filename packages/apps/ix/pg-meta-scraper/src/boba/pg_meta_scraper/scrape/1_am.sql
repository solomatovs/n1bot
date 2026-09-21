-- @name am
-- @wave 1
select
    oid,
    amname,
    xmin::text as row_xmin
from
    pg_am;
-- @verify
select
    oid,
    xmin::text as row_xmin
from
    pg_am;
