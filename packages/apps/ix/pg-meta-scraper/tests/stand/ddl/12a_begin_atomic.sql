create function dm.customer_orders(p_customer bigint) returns setof dm.orders
language sql
begin atomic
    select * from dm.orders where customer_id = p_customer;
end;
