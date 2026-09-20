-- @min 80300
create sequence dm.invoice_seq;
create table dm.invoices (
    no        bigint  primary key default nextval('dm.invoice_seq'),
    order_id  bigint  not null references dm.orders (id)
);
