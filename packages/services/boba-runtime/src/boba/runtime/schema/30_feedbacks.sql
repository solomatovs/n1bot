create table if not exists {schema}.feedbacks (
    id        uuid primary key,
    for_id    uuid not null,
    value     smallint not null,
    thread_id uuid,
    comment   text
);

create index if not exists idx_feedbacks_for_id
    on {schema}.feedbacks (for_id);
