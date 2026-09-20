-- @max 99999
create table dm.events (
    ts    timestamptz not null,
    kind  text        not null
);
create table dm.events_2026 (check (ts >= '2026-01-01' and ts < '2027-01-01')) inherits (dm.events);
create index events_2026__kind on dm.events_2026 (kind, ts);
