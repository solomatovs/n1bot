-- @min 80300
create table dm.order_items (
    order_id    bigint   not null,
    line_no     smallint not null,
    sku         text     not null references dm.products (sku),
    qty         int      not null check (qty > 0),
    unit_price  numeric  not null,
    primary key (order_id, line_no),
    foreign key (order_id, line_no) references dm.orders (id, line_no) on delete cascade
);
