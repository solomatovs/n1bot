-- @name opclass
-- @wave 1
-- @key oid
select
    oid,
    opcname,
    opcmethod,
    opcintype,
    xmin::text as row_xmin
from
    pg_opclass;
-- @verify
select
    oid,
    xmin::text as row_xmin
from
    pg_opclass;
