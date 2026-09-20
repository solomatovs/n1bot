-- @only gp
create table dm.customers (
    id          bigserial  primary key,
    email       dm.email_t not null,
    region      text,
    manager_id  bigint     references dm.customers (id) on delete set null
) distributed by (id);
create unique index customers_email_key on dm.customers (id, email);
create index customers__lower_email on dm.customers (lower(email));
