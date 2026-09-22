create table dm.events (
    ts    timestamptz not null,
    kind  text        not null,
    payload jsonb
) partition by range (ts);
create table dm.events_2026 partition of dm.events
    for values from ('2026-01-01') to ('2027-01-01')
    partition by list (kind);
create table dm.events_2026_click partition of dm.events_2026 for values in ('click');
create index events_2026_click__kind on dm.events_2026_click (kind, ts);
