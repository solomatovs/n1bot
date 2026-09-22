select
    oid,
    ev_class,
    rulename,
    ev_type,
    xmin::text as row_xmin
from
    pg_rewrite
where
    ev_class = any(%(rels)s::oid[])
