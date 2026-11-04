create extension if not exists vector;
create extension if not exists pg_trgm;
create extension if not exists unaccent;
create extension if not exists btree_gin;

create or replace function {schema}.immutable_unaccent(text)
returns text language sql immutable parallel safe strict as $fn$
    select unaccent('unaccent', $1)
$fn$;
