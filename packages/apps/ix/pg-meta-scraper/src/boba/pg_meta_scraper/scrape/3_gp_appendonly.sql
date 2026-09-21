-- @name gp_appendonly
-- @wave 3
-- @params rels
-- @only gp
-- @max 99999
select
    relid,
    columnstore,
    xmin::text as row_xmin
from
    pg_appendonly
where
    relid = any(%(rels)s::oid[]);
-- @verify
select
    relid,
    xmin::text as row_xmin
from
    pg_appendonly
where
    relid = any(%(rels)s::oid[]);
