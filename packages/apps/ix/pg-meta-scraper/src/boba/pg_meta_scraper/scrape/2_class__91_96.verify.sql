select
    oid,
    xmin::text as row_xmin
from
    pg_class
where
    relnamespace = any(%(schemas)s::oid[])
    and relkind in ('r', 'p', 'v', 'm', 'f', 'S', 'i', 'I', 'c');
