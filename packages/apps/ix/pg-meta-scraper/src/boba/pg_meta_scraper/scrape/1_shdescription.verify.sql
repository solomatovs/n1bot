select
    objoid,
    classoid,
    xmin::text as row_xmin
from
    pg_shdescription
where
    objoid = (select oid from pg_database where datname = current_database());
