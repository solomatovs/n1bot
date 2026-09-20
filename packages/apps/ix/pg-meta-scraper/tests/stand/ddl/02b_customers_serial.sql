-- @max 99999
-- @not gp
create table dm.customers (
    id          bigserial  primary key,
    email       dm.email_t not null unique,
    region      text,
    manager_id  bigint     references dm.customers (id) on delete set null
);
create index customers__lower_email on dm.customers (lower(email));
