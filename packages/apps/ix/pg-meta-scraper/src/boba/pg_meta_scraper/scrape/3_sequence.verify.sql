select
    seqrelid,
    xmin::text as row_xmin
from
    pg_sequence
where
    seqrelid = any(%(rels)s::oid[])
