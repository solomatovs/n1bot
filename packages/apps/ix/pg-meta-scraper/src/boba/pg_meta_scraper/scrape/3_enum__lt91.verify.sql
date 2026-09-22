select
    oid,
    xmin::text as row_xmin
from
    pg_enum
where
    enumtypid = any(%(types)s::oid[]);
