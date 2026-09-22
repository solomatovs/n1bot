create table edge_demo.order_items (
    order_id  number(12) not null references edge_demo.orders (id),
    line_no   number(4) not null,
    sku       varchar2(40) not null,
    qty       number(8) default 1 not null,
    price     number(18, 2) not null,
    constraint order_items_pk primary key (order_id, line_no)
)
organization index
