-- @name enum
-- @wave 3
-- @params types
-- @min 90100
select
    oid,
    enumtypid,
    enumlabel,
    enumsortorder,
    xmin::text as row_xmin
from
    pg_enum
where
    enumtypid = any(%(types)s::oid[]);
-- @verify
select
    oid,
    xmin::text as row_xmin
from
    pg_enum
where
    enumtypid = any(%(types)s::oid[]);
