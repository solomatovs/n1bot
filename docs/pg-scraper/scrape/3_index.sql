-- @name index
-- @wave 3
-- @params rels
-- @key indexrelid
-- @min 110000
select indexrelid, indrelid, indnatts, indnkeyatts as indnkeyatts, indisunique, indisprimary, indisexclusion,
       indimmediate, indisvalid,
       array(select unnest(indkey::int2[])) as indkey,
       array(select unnest(indoption::int2[])) as indoption,
       array(select unnest(indclass::oid[])) as indclass,
       pg_get_expr(indexprs, indrelid) as indexprs, pg_get_expr(indpred, indrelid) as indpred, xmin::text as row_xmin
from pg_index
where indrelid = any($1);
-- @verify
select indexrelid, xmin::text as row_xmin
from pg_index
where indrelid = any($1);
