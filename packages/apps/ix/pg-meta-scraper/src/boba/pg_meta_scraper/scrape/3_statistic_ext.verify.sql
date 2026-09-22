select
    oid,
    xmin::text as row_xmin
from
    pg_statistic_ext
where
    stxrelid = any(%(rels)s::oid[])
