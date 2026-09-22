select
    rngtypid,
    xmin::text as row_xmin
from
    pg_range
where
    rngtypid = any(%(types)s::oid[])
