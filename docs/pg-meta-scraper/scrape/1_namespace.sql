-- @name namespace
-- @wave 1
-- @key oid
-- @collect schemas oid
select oid, nspname, nspowner, pg_get_userbyid(nspowner) as owner_name, xmin::text as row_xmin
from pg_namespace
where nspname not in ('information_schema', 'gp_toolkit')
  and nspname !~ '^pg_';
-- @verify
select oid, xmin::text as row_xmin
from pg_namespace
where nspname not in ('information_schema', 'gp_toolkit')
  and nspname !~ '^pg_';
