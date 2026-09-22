create or replace trigger edge_demo.orders_biu
before insert or update on edge_demo.orders
for each row
begin
    if :new.created_at is null then
        :new.created_at := sysdate;
    end if;
end;
