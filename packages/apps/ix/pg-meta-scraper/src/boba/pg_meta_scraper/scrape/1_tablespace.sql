-- @name tablespace
-- @wave 1
-- @key oid
select
    oid,
    spcname,
    xmin::text as row_xmin
from
    pg_tablespace;
-- @verify
select
    oid,
    xmin::text as row_xmin
from
    pg_tablespace;
