select
    oid,
    tgrelid,
    tgname,
    tgfoid,
    tgtype,
    tgenabled,
    tgconstraint,
    tgconstrrelid,
    0::oid as tgparentid,
    array(select unnest(tgattr::int2[])) as tgattr,
    xmin::text as row_xmin
from
    pg_trigger
where
    tgrelid = any(%(rels)s::oid[]) and not tgisinternal;
