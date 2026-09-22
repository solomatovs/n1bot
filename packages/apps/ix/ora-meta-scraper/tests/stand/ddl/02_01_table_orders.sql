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
