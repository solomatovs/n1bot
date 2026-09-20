-- @name constraint
-- @wave 3
-- @params rels types
-- @key oid
-- @collect constraints oid
-- @min 110000
-- @max 149999
select oid, conname, connamespace, contype, conrelid, contypid, conindid, confrelid,
       condeferrable, condeferred, convalidated as convalidated, conparentid as conparentid, conislocal, coninhcount,
       confupdtype, confdeltype, confmatchtype, conkey, confkey, conpfeqop, conexclop,
       null::int2[] as confdelsetcols, pg_get_constraintdef(oid) as definition, xmin::text as row_xmin
from pg_constraint
where conrelid = any($1) or contypid = any($2);
-- @verify
select oid, xmin::text as row_xmin
from pg_constraint
where conrelid = any($1) or contypid = any($2);
