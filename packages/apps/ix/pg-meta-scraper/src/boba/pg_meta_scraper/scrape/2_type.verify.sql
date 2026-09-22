select
    oid,
    xmin::text as row_xmin
from
    pg_type
where
    typnamespace = any(%(schemas)s::oid[]);
