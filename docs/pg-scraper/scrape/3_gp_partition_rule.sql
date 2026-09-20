-- @name gp_partition_rule
-- @wave 3
-- @params rels
-- @key oid
-- @only gp
-- @max 99999
select oid, paroid, parchildrelid, parparentrule, parname, parruleord, xmin::text as row_xmin
from pg_partition_rule
where parchildrelid = any(%(rels)s::oid[]);
-- @verify
select oid, xmin::text as row_xmin
from pg_partition_rule
where parchildrelid = any(%(rels)s::oid[]);
