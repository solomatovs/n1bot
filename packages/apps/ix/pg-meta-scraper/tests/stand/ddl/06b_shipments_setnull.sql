-- @max 149999
create table dm.shipments (
    id           bigserial primary key,
    order_id     bigint,
    order_line   smallint,
    carrier      text not null,
    foreign key (order_id, order_line) references dm.orders (id, line_no) on delete set null
);
