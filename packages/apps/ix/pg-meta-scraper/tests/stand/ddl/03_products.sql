create table dm.products (
    id    bigserial primary key,
    sku   text      not null,
    price numeric   not null check (price > 0)
);
create unique index products__sku_uk on dm.products (sku);
