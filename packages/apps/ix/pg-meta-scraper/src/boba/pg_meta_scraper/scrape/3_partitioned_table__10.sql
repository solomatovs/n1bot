select
    partrelid,
    partstrat,
    partnatts,
    array(select unnest(partattrs::int2[])) as partattrs,
    array(select unnest(partclass::oid[])) as partclass,
    0::oid as partdefid,
    xmin::text as row_xmin
from
    pg_partitioned_table
where
    partrelid = any(%(rels)s::oid[])
