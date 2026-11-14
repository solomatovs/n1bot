-- Journal provenance checks for newly appended logical records: session, from_seq, through_seq.
with
entries as (
    select
        seq, record_id, copied_from_id, kind, category, request_id, payload
    from
        history_sql.records
    where 1=1
        and session_id = %s
        and seq >= %s
        and seq <= %s
),
links as (
    select
        seq, record_id, payload ->> 'related_message_id' as target_id,
        null::text as target_category, null::text as target_kind,
        'invalid_resource_reference' as code
    from
        entries
    where 1=1
        and payload ->> 'related_message_id' is not null
    union all
    select
        seq, record_id, request_id, 'request', null, 'invalid_response_reference'
    from
        entries
    where 1=1
        and payload ->> 'role' = 'assistant'
    union all
    select
        seq, record_id, payload ->> 'elicitation_id', 'operational',
        'elicitation_request', 'invalid_elicitation_reference'
    from
        entries
    where 1=1
        and payload ->> 'elicitation_id' is not null
    union all
    select
        seq, record_id, copied_from_id, category, kind, 'invalid_copy_reference'
    from
        entries
    where 1=1
        and copied_from_id is not null
),
issues as (
    select
        l.seq, l.record_id, l.code,
        'Provenance must reference an earlier record: ' || coalesce(l.target_id, '<null>') as message
    from
        links l
        left join history_sql.records p
            on p.session_id = %s
            and p.record_id = l.target_id
    where 1=1
        and (
            p.seq is null
            or p.seq >= l.seq
            or (l.target_category is not null and p.category <> l.target_category)
            or (l.target_kind is not null and p.kind <> l.target_kind)
        )
)
select
    coalesce(
        jsonb_agg(
            jsonb_build_object(
                'code', code,
                'seq', seq,
                'record_id', record_id,
                'message', message
            ) order by seq, code
        ), '[]'
    ) as errors
from
    issues;
