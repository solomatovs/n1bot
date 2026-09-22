select
    oid,
    paroid,
    parchildrelid,
    parparentrule,
    parname,
    parruleord,
    xmin::text as row_xmin
from
    pg_partition_rule
where
    parchildrelid = any(%(rels)s::oid[]);
