-- @name attribute
-- @wave 3
-- @params rels
-- @min 100000
-- @max 119999
select
    attrelid,
    attnum,
    attname,
    atttypid,
    atttypmod,
    format_type(atttypid, atttypmod) as data_type,
    attnotnull,
    atthasdef,
    attidentity as attidentity,
    '' as attgenerated,
    xmin::text as row_xmin
from
    pg_attribute
where
    attrelid = any(%(rels)s::oid[]) and attnum > 0 and not attisdropped;
-- @verify
select
    attrelid,
    attnum,
    xmin::text as row_xmin
from
    pg_attribute
where
    attrelid = any(%(rels)s::oid[]) and attnum > 0 and not attisdropped;
