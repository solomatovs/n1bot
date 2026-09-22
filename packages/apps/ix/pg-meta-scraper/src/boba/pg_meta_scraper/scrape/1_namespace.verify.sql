select
    oid,
    xmin::text as row_xmin
from
    pg_namespace
where
    nspname not in ('information_schema', 'gp_toolkit') and nspname !~ '^pg_'
