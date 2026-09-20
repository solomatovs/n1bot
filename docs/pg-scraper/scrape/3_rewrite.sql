-- @name rewrite
-- @wave 3
-- @params rels
-- @key oid
select oid, ev_class, rulename, ev_type, xmin::text as row_xmin
from pg_rewrite
where ev_class = any($1);
-- @verify
select oid, xmin::text as row_xmin
from pg_rewrite
where ev_class = any($1);
