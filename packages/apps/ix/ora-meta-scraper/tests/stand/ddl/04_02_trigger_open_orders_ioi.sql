create or replace trigger edge_demo.open_orders_ioi
instead of insert on edge_demo.open_orders
begin
    insert into edge_demo.orders (id, customer_id, amount, status, created_at)
    values (:new.id, :new.customer_id, :new.amount, 'open', :new.created_at);
end;
