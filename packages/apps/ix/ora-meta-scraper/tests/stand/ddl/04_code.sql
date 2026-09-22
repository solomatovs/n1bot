create or replace trigger edge_demo.orders_biu
before insert or update on edge_demo.orders
for each row
begin
    if :new.created_at is null then
        :new.created_at := sysdate;
    end if;
end;
/
create or replace trigger edge_demo.open_orders_ioi
instead of insert on edge_demo.open_orders
begin
    insert into edge_demo.orders (id, customer_id, amount, status, created_at)
    values (:new.id, :new.customer_id, :new.amount, 'open', :new.created_at);
end;
/
create or replace function edge_demo.order_total(p_order_id in number) return number
is
    v_total number;
begin
    select sum(qty * price) into v_total from edge_demo.order_items where order_id = p_order_id;
    return nvl(v_total, 0);
end;
/
create or replace procedure edge_demo.close_order(p_order_id in number)
is
begin
    update edge_demo.orders set status = 'closed' where id = p_order_id;
end;
/
create or replace package edge_demo.order_api as
    procedure close_all;
    function count_open return number;
end order_api;
/
create or replace package body edge_demo.order_api as
    procedure close_all is
    begin
        update edge_demo.orders set status = 'closed' where status = 'open';
    end;
    function count_open return number is
        v_count number;
    begin
        select count(*) into v_count from edge_demo.orders where status = 'open';
        return v_count;
    end;
end order_api;
/
create or replace type edge_demo.money_t as object (
    amount    number(18, 2),
    currency  varchar2(3)
)
/
