-- @name range
-- @wave 3
-- @params types
-- @min 90200
select
    rngtypid,
    rngsubtype,
    format_type(rngsubtype, null) as subtype_name,
    xmin::text as row_xmin
from
    pg_range
where
    rngtypid = any(%(types)s::oid[]);
-- @verify
select
    rngtypid,
    xmin::text as row_xmin
from
    pg_range
where
    rngtypid = any(%(types)s::oid[]);
