-- Journal provenance checks for newly appended logical records: session, from_seq, through_seq.
with
params as (
    select
        $1::text as session_id,
        $2::bigint as from_seq,
        $3::bigint as through_seq
),
entries as (
    select
        seq, record_id, kind, category, role, request_id, copied_from_id, payload
    from
        boba.records
    where 1=1
        and session_id = (select session_id from params)
        and seq >= (select from_seq from params)
        and seq <= (select through_seq from params)
),
links as (
    select
        seq, record_id, payload ->> 'permission_request_id' as target_id,
        'operational'::text as target_category, 'permission_request'::text as target_kind,
        'invalid_permission_reference' as code
    from
        entries
    where 1=1
        and kind = 'permission_response'
    union all
    select
        seq, record_id, payload ->> 'source_record_id' as target_id,
        'input'::text as target_category, null::text as target_kind,
        'invalid_admission_reference' as code
    from
        entries
    where 1=1
        and category = 'admission'
    union all
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
        and (role = 'assistant' or (
            request_id is not null
            and category not in ('setting', 'prompt', 'context', 'request_context', 'request_message')
        ))
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
        left join boba.records p
            on p.session_id = (select session_id from params)
            and p.record_id = l.target_id
    where 1=1
        and (
            p.seq is null
            or p.seq >= l.seq
            or (l.target_category is not null and p.category <> l.target_category)
            or (l.target_kind is not null and p.kind <> l.target_kind)
        )
    union all
    select
        e.seq, e.record_id, 'invalid_permission_option',
        'Selected option must belong to the saved permission request'
    from
        entries e
        join boba.records q
            on q.session_id = (select session_id from params)
            and q.record_id = e.payload ->> 'permission_request_id'
        left join lateral jsonb_array_elements(q.payload -> 'options') o(value)
            on o.value ->> 'option_id' = e.payload ->> 'option_id'
    where 1=1
        and e.kind = 'permission_response'
        and e.payload ->> 'action' = 'selected'
        and o.value is null
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
