create or replace function edge_demo.order_total(p_order_id in number) return number
is
    v_total number;
begin
    select sum(qty * price) into v_total from edge_demo.order_items where order_id = p_order_id;
    return nvl(v_total, 0);
end;
