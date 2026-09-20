-- @min 120000
alter table dm.order_items add column total numeric generated always as (qty * unit_price) stored;
