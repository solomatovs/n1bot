-- @min 80300
create function dm.calc_total(p_order bigint, p_rate numeric) returns numeric
language plpgsql as $$
begin
    return (select sum(unit_price * qty) * p_rate from dm.order_items where order_id = p_order);
end $$;
create table dm.orders_audit (
    id        bigserial primary key,
    order_id  bigint      not null,
    changed   timestamptz not null default now(),
    old_amount numeric,
    new_amount numeric
);
create function dm.orders_audit_fn() returns trigger
language plpgsql as $$
begin
    insert into dm.orders_audit (order_id, old_amount, new_amount)
    values (new.id, old.amount, new.amount);
    return new;
end $$;
create trigger orders__audit
    after update of amount, status on dm.orders
    for each row execute procedure dm.orders_audit_fn();
