select
    indexrelid,
    indrelid,
    indnatts,
    indnatts as indnkeyatts,
    indisunique,
    indisprimary,
    indisexclusion,
    indimmediate,
    indisvalid,
    array(select unnest(indkey::int2[])) as indkey,
    array(select unnest(indoption::int2[])) as indoption,
    array(select unnest(indclass::oid[])) as indclass,
    pg_get_expr(indexprs, indrelid) as indexprs,
    pg_get_expr(indpred, indrelid) as indpred,
    xmin::text as row_xmin
from
    pg_index
where
    indrelid = any(%(rels)s::oid[])
