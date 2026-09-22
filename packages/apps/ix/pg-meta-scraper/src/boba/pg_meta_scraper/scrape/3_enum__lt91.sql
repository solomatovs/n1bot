select
    oid,
    enumtypid,
    enumlabel,
    oid::int4::float8 as enumsortorder,
    xmin::text as row_xmin
from
    pg_enum
where
    enumtypid = any(%(types)s::oid[]);
