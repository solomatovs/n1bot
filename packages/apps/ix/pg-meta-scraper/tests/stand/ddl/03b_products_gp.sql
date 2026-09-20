-- @only gp
create table dm.products (
    id    bigserial,
    sku   text      primary key,
    price numeric   not null check (price > 0)
) distributed by (sku);
create table dm.products_noconstraint (sku text not null) distributed by (sku);
create unique index products_noconstraint__sku_uk on dm.products_noconstraint (sku);
create table dm.fk_to_index_only (sku text references dm.products_noconstraint (sku)) distributed by (sku);
