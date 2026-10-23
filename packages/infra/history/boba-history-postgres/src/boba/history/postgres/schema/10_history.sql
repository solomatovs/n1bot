create table if not exists {schema}.session (
    id         text        primary key,
    created_at timestamptz not null
);

create table if not exists {schema}.history (
    session_id  text        not null references {schema}.session on delete cascade,
    seq         integer     not null,
    type        text        not null,
    recorded_at timestamptz not null,
    record      jsonb       not null,
    primary key (session_id, seq)
);

comment on table {schema}.session is
'Сессии агента. Строку заводит первая дозапись сессии, а удаление строки уносит все записи её истории.';

comment on column {schema}.session.id is
'Идентификатор сессии, который агент получает от клиента; у сессии треда чата он совпадает с id треда.';

comment on column {schema}.session.created_at is
'Время первой записи сессии.';

comment on table {schema}.history is
'История сессий агента: по строке на запись журнала в порядке seq внутри сессии. Общие для всех записей поля лежат колонками, всё остальное содержимое записи лежит в record.';

comment on column {schema}.history.seq is
'Порядковый номер записи в сессии, начиная с 1; его выдаёт сессия агента.';

comment on column {schema}.history.type is
'Вид записи: user, assistant, attachment, system, queue-operation, permission-mode, permission-rule или api-request.';

comment on column {schema}.history.recorded_at is
'Время, когда агент сделал запись.';

comment on column {schema}.history.record is
'Остальные поля записи в том виде, в каком агент пишет их в журнал JSONL, но без идентификатора сессии, seq, вида и времени: они лежат в колонках этой строки.';
