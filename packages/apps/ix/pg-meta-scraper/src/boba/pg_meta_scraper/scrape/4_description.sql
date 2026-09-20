-- @name description
-- @wave 4
-- @params rels procs types constraints schemas triggers statistics
-- @key objoid, classoid, objsubid
select objoid, classoid, objsubid, description, xmin::text as row_xmin
from pg_description
where objoid = any(%(rels)s::oid[]) or objoid = any(%(procs)s::oid[]) or objoid = any(%(types)s::oid[]) or objoid = any(%(constraints)s::oid[])
   or objoid = any(%(schemas)s::oid[]) or objoid = any(%(triggers)s::oid[]) or objoid = any(%(statistics)s::oid[]);
-- @verify
select objoid, classoid, objsubid, xmin::text as row_xmin
from pg_description
where objoid = any(%(rels)s::oid[]) or objoid = any(%(procs)s::oid[]) or objoid = any(%(types)s::oid[]) or objoid = any(%(constraints)s::oid[])
   or objoid = any(%(schemas)s::oid[]) or objoid = any(%(triggers)s::oid[]) or objoid = any(%(statistics)s::oid[]);
