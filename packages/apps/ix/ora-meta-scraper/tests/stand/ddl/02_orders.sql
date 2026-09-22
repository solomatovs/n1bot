create table edge_demo.orders (
    id           number(12) not null,
    customer_id  number(10) not null,
    amount       number(18, 2) not null,
    status       varchar2(10) default 'open' not null,
    created_at   date not null,
    payload      blob,
    constraint orders_pk primary key (id),
    constraint orders_customer_fk foreign key (customer_id)
        references edge_demo.customers (id) on delete cascade,
    constraint orders_amount_ck check (amount >= 0)
)
/
comment on table edge_demo.orders is 'Заказы'
/
comment on column edge_demo.orders.customer_id is 'Клиент, см. customers'
/
create index edge_demo.orders_customer_ix on edge_demo.orders (customer_id, created_at desc)
/
create bitmap index edge_demo.orders_status_bx on edge_demo.orders (status)
/
create table edge_demo.order_items (
    order_id  number(12) not null references edge_demo.orders (id),
    line_no   number(4) not null,
    sku       varchar2(40) not null,
    qty       number(8) default 1 not null,
    price     number(18, 2) not null,
    constraint order_items_pk primary key (order_id, line_no)
)
organization index
/
create global temporary table edge_demo.order_staging (
    order_id  number(12),
    note      varchar2(400)
) on commit delete rows
/
