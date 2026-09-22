select
    oid,
    xmin::text as row_xmin
from
    pg_partition
where
    parrelid = any(%(rels)s::oid[])
