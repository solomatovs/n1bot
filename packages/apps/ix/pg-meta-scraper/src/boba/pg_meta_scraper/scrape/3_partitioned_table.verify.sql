select
    partrelid,
    xmin::text as row_xmin
from
    pg_partitioned_table
where
    partrelid = any(%(rels)s::oid[]);
