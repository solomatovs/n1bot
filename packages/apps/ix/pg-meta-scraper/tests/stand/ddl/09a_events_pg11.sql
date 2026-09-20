-- @min 110000
create table dm.events (
    ts    timestamptz not null,
    kind  text        not null,
    payload jsonb
) partition by range (ts);
create index events__kind on dm.events (kind, ts);
create table dm.events_2026 partition of dm.events
    for values from ('2026-01-01') to ('2027-01-01')
    partition by list (kind);
create table dm.events_2026_click partition of dm.events_2026 for values in ('click');
create table dm.events_2026_other partition of dm.events_2026 default;
create table dm.events_default partition of dm.events default;
