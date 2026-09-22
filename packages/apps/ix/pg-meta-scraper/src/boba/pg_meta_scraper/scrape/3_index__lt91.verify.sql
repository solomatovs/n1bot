select
    indexrelid,
    xmin::text as row_xmin
from
    pg_index
where
    indrelid = any(%(rels)s::oid[])
