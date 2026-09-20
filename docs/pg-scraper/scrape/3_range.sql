-- @name range
-- @wave 3
-- @params types
-- @key rngtypid
-- @min 90200
select rngtypid, rngsubtype, format_type(rngsubtype, null) as subtype_name, xmin::text as row_xmin
from pg_range
where rngtypid = any($1);
-- @verify
select rngtypid, xmin::text as row_xmin
from pg_range
where rngtypid = any($1);
