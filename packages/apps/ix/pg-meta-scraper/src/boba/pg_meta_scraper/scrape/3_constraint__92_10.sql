-- @name constraint
-- @wave 3
-- @params rels types
-- @collect constraints oid
-- @min 90200
-- @max 109999
select
    oid,
    conname,
    connamespace,
    contype,
    conrelid,
    contypid,
    conindid,
    confrelid,
    condeferrable,
    condeferred,
    convalidated as convalidated,
    0::oid as conparentid,
    conislocal,
    coninhcount,
    confupdtype,
    confdeltype,
    confmatchtype,
    conkey,
    confkey,
    conpfeqop,
    conexclop,
    null::int2[] as confdelsetcols,
    pg_get_constraintdef(oid) as definition,
    xmin::text as row_xmin
from
    pg_constraint
where
    conrelid = any(%(rels)s::oid[]) or contypid = any(%(types)s::oid[]);
-- @verify
select
    oid,
    xmin::text as row_xmin
from
    pg_constraint
where
    conrelid = any(%(rels)s::oid[]) or contypid = any(%(types)s::oid[]);
