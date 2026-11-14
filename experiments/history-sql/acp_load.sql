-- Нагрузочные истории для ACP: четыре отдельных профиля и 512 клиентских сессий.
-- Запускать в экспериментальной БД после schema.sql; заменяет только перечисленные тестовые сессии.
-- Цикл: user -> граница -> assistant/tool call -> result -> notice -> usage -> вопрос
-- -> ответ -> sampling -> завершение. Весь текст и структуры сохраняются в records.
begin;

delete
from
    history_sql.records
where 1=1
    and session_id in ('sql-acp-load-1000', 'sql-acp-load-10000', 'sql-acp-load-50000', 'sql-acp-load-commands');

insert into history_sql.records (
    session_id, seq, record_id, created_at, version, kind, category, key, operation, request_id, payload
)
with
sizes(session_id, rounds) as (
    values
        ('sql-acp-load-1000', 100),
        ('sql-acp-load-10000', 1000),
        ('sql-acp-load-50000', 5000)
),
initial(seq, record_id, kind, category, key, payload) as (
    values
        (1, 'model', 'model', 'setting', 'model',
            '{"role":null,"value":{"model":"test-model"},"provider":"openai"}'::jsonb),
        (2, 'permission', 'permission_state', 'operational', null,
            '{"role":null,"mode":"default","rules":[]}'::jsonb)
),
rounds as (
    select
        s.session_id, n.number,
        'r-' || n.number as request_id,
        'call-' || n.number as call_id,
        'question-' || n.number as question_id,
        repeat('Тестовый сохранённый текст. ', 16) as text
    from
        sizes s
        cross join lateral generate_series(1, s.rounds) n(number)
),
entries as (
    select
        s.session_id, i.seq, i.record_id, i.kind, i.category, i.key,
        null::text as request_id, i.payload
    from
        sizes s
        cross join initial i
    union all
    select
        r.session_id, 2 + (r.number - 1) * 10 + e.part_no,
        e.record_id, e.kind, e.category, e.key, e.request_id, e.payload
    from
        rounds r
        cross join lateral (
            values
                (1, 'user-' || r.number, 'user_message', 'message', null::text, null::text,
                    jsonb_build_object('role', 'user', 'content', jsonb_build_array(
                        jsonb_build_object('kind', 'text', 'text', r.text)))),
                (2, r.request_id, 'llm_request_boundary', 'request', null, null,
                    jsonb_build_object('role', null)),
                (3, 'assistant-' || r.number, 'assistant_message', 'message', null, r.request_id,
                    jsonb_build_object('role', 'assistant', 'completion', 'complete', 'content',
                        jsonb_build_array(
                            jsonb_build_object('kind', 'reasoning', 'text', 'Проверю данные.',
                                'signature', null, 'format', null),
                            jsonb_build_object('kind', 'text', 'text', r.text),
                            jsonb_build_object('kind', 'tool_call', 'call_id', r.call_id,
                                'name', 'mcp__workspace__read', 'arguments', jsonb_build_object('path', 'app.py'))))),
                (4, 'result-' || r.number, 'tool_result', 'message', null, null,
                    jsonb_build_object('role', 'tool', 'call_id', r.call_id, 'is_error', false,
                        'content', jsonb_build_array(jsonb_build_object('kind', 'text', 'text', r.text)))),
                (5, 'notice-' || r.number, 'notice', 'operational', null, null,
                    jsonb_build_object('role', null, 'level', 'warning', 'content',
                        jsonb_build_array(jsonb_build_object('kind', 'text', 'text', 'Повтор операции.')))),
                (6, 'usage-' || r.number, 'token_usage', 'operational', null, r.request_id,
                    jsonb_build_object('role', null, 'usage', jsonb_build_object(
                        'input_tokens', 100, 'output_tokens', 30, 'reasoning_tokens', 5,
                        'cache_read_tokens', 0, 'cache_write_tokens', 0))),
                (7, r.question_id, 'elicitation_request', 'operational', null, null,
                    jsonb_build_object('role', null, 'source', 'agent.demo', 'message', 'Продолжить?',
                        'mode', 'form', 'related_message_id', null, 'call_id', null, 'url', null,
                        'requested_schema', jsonb_build_object('type', 'object', 'properties',
                            jsonb_build_object('confirm', jsonb_build_object('type', 'boolean'))))),
                (8, 'answer-' || r.number, 'elicitation_response', 'operational', null, null,
                    jsonb_build_object('role', null, 'elicitation_id', r.question_id, 'action', 'accept',
                        'content', jsonb_build_object('confirm', true))),
                (9, 'sampling-' || r.number, 'sampling', 'setting', 'sampling', null,
                    jsonb_build_object('role', null, 'value', jsonb_build_object('temperature', 0.2))),
                (10, 'end-' || r.number, 'turn_end', 'operational', null, null,
                    jsonb_build_object('role', null, 'turn_id', r.request_id, 'outcome', 'completed',
                        'duration_ms', 100, 'error', null))
        ) e(part_no, record_id, kind, category, key, request_id, payload)
)
select
    session_id, seq, record_id, '2026-11-14 12:00:00+00'::timestamptz,
    '0.0.33.dev4', kind, category, key, 'set', request_id, payload
from
    entries
order by
    session_id, seq;

-- Отдельный профиль: много результатов slash-команд, включая пустые.
insert into history_sql.records (
    session_id, seq, record_id, created_at, version, kind, category, operation, payload
)
select
    'sql-acp-load-commands', (n.number - 1) * 2 + e.part_no, e.record_id,
    '2026-11-14 12:00:00+00'::timestamptz, '0.0.33.dev4', e.kind, 'operational', 'set', e.payload
from
    generate_series(1, 1000) n(number)
    cross join lateral (
        values
            (1, 'command-' || n.number, 'slash_command', jsonb_build_object(
                'role', null, 'name', 'demo', 'text', '/demo', 'arguments', '{}'::jsonb,
                'owner', jsonb_build_object('kind', 'agent'))),
            (2, 'result-' || n.number, 'slash_command_result', jsonb_build_object(
                'role', null, 'command_id', 'command-' || n.number, 'outcome', 'succeeded',
                'content', jsonb_build_array(jsonb_build_object('kind', 'text', 'text', 'Команда выполнена.')),
                'structured_content', null, 'error', null))
    ) e(part_no, record_id, kind, payload)
order by
    n.number, e.part_no;

-- 512 клиентских сессий: 480 по 1002 записи, 28 по 10002, 4 по 50002.
-- Новые таблицы не нужны; для нагрузки используются строки той же records.
delete
from
    history_sql.records r
using
    generate_series(1, 512) n(number)
where 1=1
    and r.session_id = 'sql-acp-stress-' || lpad(n.number::text, 4, '0');

insert into history_sql.records (
    session_id, seq, record_id, copied_from_id, created_at, version,
    kind, category, key, operation, request_id, payload
)
with
sessions as (
    select
        'sql-acp-stress-' || lpad(n.number::text, 4, '0') as session_id,
        case
            when (n.number - 1) % 128 = 0 then 'sql-acp-load-50000'
            when (n.number - 1) % 16 = 0 then 'sql-acp-load-10000'
            else 'sql-acp-load-1000'
        end as source_session_id
    from
        generate_series(1, 512) n(number)
)
select
    s.session_id, r.seq, r.record_id, r.copied_from_id, r.created_at, r.version,
    r.kind, r.category, r.key, r.operation, r.request_id, r.payload
from
    sessions s
    join history_sql.records r on r.session_id = s.source_session_id
order by
    s.session_id, r.seq;

commit;
