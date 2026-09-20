-- @name database
-- @wave 1
-- @key oid
select oid, datname, datdba, pg_get_userbyid(datdba) as owner_name, encoding,
       pg_encoding_to_char(encoding) as encoding_name, datcollate, datctype, xmin::text as row_xmin
from pg_database
where datname = current_database();
-- @verify
select oid, xmin::text as row_xmin
from pg_database
where datname = current_database();
