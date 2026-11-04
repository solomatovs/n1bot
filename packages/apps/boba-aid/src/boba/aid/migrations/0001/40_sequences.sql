create temporary sequence live_events_seq as bigint;
create sequence if not exists {schema}.live_events_seq as bigint;
select pg_temp.migration_sequence(
    format('%I.live_events_seq', {schema_name_lit})::regclass,
    'pg_temp.live_events_seq'::regclass,
    (select max(seq) from {schema}.live_events)
);
