create table dm.gp_sales (
    id      bigint,
    region  text,
    day     date,
    amount  numeric
) distributed by (region, day);
create table dm.gp_ao (id bigint, payload text) with (appendonly=true, orientation=column) distributed by (id);
create table dm.gp_random (id bigint) distributed randomly;
create table dm.gp_part (id bigint, day date, amount numeric)
    distributed by (id)
    partition by range (day) (start ('2026-01-01') end ('2026-04-01') every (interval '1 month'), default partition other);
create external table dm.gp_ext (id bigint, name text) location ('file://localhost/tmp/x.csv') format 'csv';
