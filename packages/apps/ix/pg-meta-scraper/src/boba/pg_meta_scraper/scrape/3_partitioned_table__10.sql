-- @name partitioned_table
-- @wave 3
-- @params rels
-- @key partrelid
-- @min 100000
-- @max 109999
select partrelid, partstrat, partnatts,
       array(select unnest(partattrs::int2[])) as partattrs,
       array(select unnest(partclass::oid[])) as partclass, 0::oid as partdefid, xmin::text as row_xmin
from pg_partitioned_table
where partrelid = any(%(rels)s::oid[]);
-- @verify
select partrelid, xmin::text as row_xmin
from pg_partitioned_table
where partrelid = any(%(rels)s::oid[]);
