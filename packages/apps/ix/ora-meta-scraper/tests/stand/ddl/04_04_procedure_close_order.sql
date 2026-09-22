create or replace procedure edge_demo.close_order(p_order_id in number)
is
begin
    update edge_demo.orders set status = 'closed' where id = p_order_id;
end;
