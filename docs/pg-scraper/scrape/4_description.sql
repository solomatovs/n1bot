-- @name description
-- @wave 4
-- @params rels procs types constraints schemas triggers statistics
-- @key objoid, classoid, objsubid
select objoid, classoid, objsubid, description, xmin::text as row_xmin
from pg_description
where objoid = any($1) or objoid = any($2) or objoid = any($3) or objoid = any($4)
   or objoid = any($5) or objoid = any($6) or objoid = any($7);
-- @verify
select objoid, classoid, objsubid, xmin::text as row_xmin
from pg_description
where objoid = any($1) or objoid = any($2) or objoid = any($3) or objoid = any($4)
   or objoid = any($5) or objoid = any($6) or objoid = any($7);
