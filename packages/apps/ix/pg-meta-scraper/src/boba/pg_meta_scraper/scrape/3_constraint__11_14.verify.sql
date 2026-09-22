select
    oid,
    xmin::text as row_xmin
from
    pg_constraint
where
    conrelid = any(%(rels)s::oid[]) or contypid = any(%(types)s::oid[])
