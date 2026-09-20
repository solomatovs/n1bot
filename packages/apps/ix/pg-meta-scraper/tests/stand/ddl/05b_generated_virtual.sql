-- @min 180000
alter table dm.order_items add column total_x2 numeric generated always as (qty * unit_price * 2) virtual;
