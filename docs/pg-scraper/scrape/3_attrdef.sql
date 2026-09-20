-- @name attrdef
-- @wave 3
-- @params rels
-- @key oid
-- @collect attrdefs oid
select oid, adrelid, adnum, pg_get_expr(adbin, adrelid) as expr, xmin::text as row_xmin
from pg_attrdef
where adrelid = any(%(rels)s::oid[]);
-- @verify
select oid, xmin::text as row_xmin
from pg_attrdef
where adrelid = any(%(rels)s::oid[]);
