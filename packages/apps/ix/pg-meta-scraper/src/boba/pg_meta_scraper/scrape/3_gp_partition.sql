select
    oid,
    parrelid,
    parkind,
    parlevel,
    array(select unnest(paratts::int2[])) as paratts,
    xmin::text as row_xmin
from
    pg_partition
where
    parrelid = any(%(rels)s::oid[]);
