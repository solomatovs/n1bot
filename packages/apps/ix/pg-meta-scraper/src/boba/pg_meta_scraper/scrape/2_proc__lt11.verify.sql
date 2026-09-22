select
    oid,
    xmin::text as row_xmin
from
    pg_proc
where
    pronamespace = any(%(schemas)s::oid[])
