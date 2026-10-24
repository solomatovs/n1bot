-- Третий шаг перехода чата на процесс-агента: приведение копии старой базы,
-- накатанной restore.sh, к тому виду, который ждёт новое приложение. Скрипт
-- выполняется в НОВОЙ базе и меняет её на месте; старая база не участвует.
--
-- Что делает:
--   * переводит беседы тредов из чекпойнтов langgraph в историю агента
--     (таблицы session и session_records, сессия на тред с тем же id) и пишет
--     каждому треду чата ключ threads.meta.agent_session, по которому чат
--     находит сессию треда;
--   * переводит оценки ответов (feedbacks.for_id) на id шагов, которые новый
--     чат рисует при воспроизведении беседы агентом;
--   * приводит таблицы чата к раскладке, которую создаёт новое приложение;
--   * удаляет таблицы, которые новому приложению не нужны: чекпойнты
--     langgraph и вложения elements;
--   * раскладывает таблицы по схемам компонентов: чат — в aid, история
--     агента — в boba, база знаний и описания (плагины boba-mcp) — в mcp.
-- Содержимое остальных таблиц (база знаний, соединения, гранты, ix, studio)
-- не меняется: их раскладку приложение и индексаторы ведут сами при старте.
--
-- Переменные psql (передаются через -v). Схемы копии называются как в базе
-- dev; новое приложение раскладывает таблицы по трём схемам своих
-- компонентов: aid — чат, boba — агент, mcp — сервис boba-mcp и его
-- плагины. Имена схем можно не передавать, тогда берутся умолчания:
--   chainlit_schema    — схема данных чата dev, по умолчанию chainlit;
--   checkpoints_schema — схема чекпойнтов langgraph, по умолчанию checkpoints;
--   history_schema     — схема истории агента, если она была в старой базе,
--                        по умолчанию agent_history;
--   kb_schema          — схема базы знаний, по умолчанию kb;
--   describer_schema   — схема инструмента описаний, по умолчанию describer;
--   aid_schema         — схема чата в новой базе, по умолчанию aid;
--   boba_schema        — схема агента в новой базе, по умолчанию boba;
--   mcp_schema         — схема boba-mcp и плагинов в новой базе, по
--                        умолчанию mcp;
--   chat_url           — обязательна: адрес чата глазами агента, буква в
--                        букву значение [agent].public_url общего конфига
--                        нового чата после подстановки. По умолчанию это
--                        внутренний адрес процесса чата
--                        http://127.0.0.1:<порт><префикс>, а не публичное
--                        имя за nginx; у compose с портом и префиксом по
--                        умолчанию — http://127.0.0.1:8501/boba. Из него
--                        строятся ссылки на вложения вопросов.
-- Схемы ix (индексаторы) и studio (automation, catalog) скрипт не трогает.
--
-- Запуск:
--   PGDATABASE=<новая база> psql -v chat_url=http://127.0.0.1:8501/boba \
--        -f migrate.sql
--
-- Весь перевод идёт одной транзакцией: проверка, рабочие функции, извлечение,
-- перевод, загрузка, привязка тредов, перевод оценок, сверка, приведение
-- таблиц, удаление ненужных таблиц, раскладка по схемам и удаление рабочей
-- схемы history_migrate. Любая
-- ошибка или несошедшаяся сверка откатывает всё. Повторный запуск на уже
-- приведённой базе ничего не меняет: чекпойнтов в ней нет, и скрипт сразу
-- завершается. Отката нет: старая база не тронута, новая удаляется целиком.

\set ON_ERROR_STOP on
\set QUIET on
\pset pager off

\if :{?chainlit_schema}
\else
    \set chainlit_schema chainlit
\endif
\if :{?checkpoints_schema}
\else
    \set checkpoints_schema checkpoints
\endif
\if :{?history_schema}
\else
    \set history_schema agent_history
\endif
\if :{?kb_schema}
\else
    \set kb_schema kb
\endif
\if :{?describer_schema}
\else
    \set describer_schema describer
\endif
\if :{?aid_schema}
\else
    \set aid_schema aid
\endif
\if :{?boba_schema}
\else
    \set boba_schema boba
\endif
\if :{?mcp_schema}
\else
    \set mcp_schema mcp
\endif
\if :{?chat_url}
\else
    \echo 'Pass the chat address the agent uses, [agent].public_url, for example -v chat_url=https://chat.example.com/boba.'
    do $$ begin raise exception 'The migration needs the psql variable chat_url, so nothing was changed.'; end $$;
\endif

-- Метка в поле version перенесённых записей: агент пишет туда свою версию,
-- перенос — эту.
\set version 'history-migrate-2026-10'

select to_regclass(format('%I.checkpoints', :'checkpoints_schema')) is null as migrated \gset
\if :migrated
    \echo 'There is no table' :checkpoints_schema'.checkpoints in this database: it is migrated already or the checkpoints schema has another name, so nothing was changed.'
    \quit
\endif

-- Схемы нового приложения в копии ещё не существуют: скрипт получает их
-- переименованием схем dev, поэтому занятое имя останавливает перенос до
-- любых изменений.
select coalesce(string_agg(name, ', ' order by name), '') as taken_schemas
from (
    values
        (:'aid_schema', :'chainlit_schema'),
        (:'boba_schema', :'history_schema'),
        (:'mcp_schema', :'kb_schema')
) as target(name, source)
where
    name <> source
    and to_regnamespace(name) is not null
\gset
select :'taken_schemas' <> '' as targets_taken \gset
\if :targets_taken
    \echo 'The schemas' :taken_schemas 'of the new application already exist in this database.'
    do $$ begin raise exception 'The target schemas must not exist before the migration (listed above), so nothing was changed. Restore the copy into a new empty database.'; end $$;
\endif

begin;

-- 1. Проверка.
--
-- Тред читается так же, как его читал чекпойнтер при открытии: последний
-- checkpoint корневого пространства имён (checkpoint_ns пустой, наибольший
-- checkpoint_id), значение канала messages той версии, которую он называет,
-- и ожидающие записи этого канала у того же checkpoint'а. Вложенные
-- пространства имён принадлежат подграфам и в беседу не входят.
--
-- Перенос останавливается, ничего не изменив, если тред нельзя перевести:
-- его id не годится в id сессии агента (от 1 до 128 латинских букв, цифр,
-- «-» и «_»), или сообщения записаны не в msgpack — другого формата разбор
-- не знает. Тред, который чат уже привязал к другой сессии агента, не
-- переносится: беседу в нём уже ведёт агент. Так бывает, если в старом
-- треде задали вопрос до переноса или правили вопрос после него, и такие
-- треды выводятся списком.

create temporary table last_checkpoint on commit drop as
select distinct on (thread_id)
    thread_id,
    checkpoint_ns,
    checkpoint_id,
    checkpoint ->> 'ts' as stamp,
    checkpoint -> 'channel_versions' ->> 'messages' as messages_version
from
    :"checkpoints_schema".checkpoints
where
    checkpoint_ns = ''
order by
    thread_id,
    checkpoint_id desc;

\echo 'Source: threads and checkpoints of the langgraph checkpointer.'
select
    count(distinct thread_id) as threads,
    count(*) as checkpoints,
    count(*) filter (where checkpoint_ns <> '') as nested_checkpoints_ignored
from
    :"checkpoints_schema".checkpoints;

create temporary table refused on commit drop as
select
    thread_id,
    'the thread id does not fit a session id of the agent (1 to 128 letters, digits, - or _)' as reason
from
    last_checkpoint
where
    thread_id !~ '^[A-Za-z0-9_-]{1,128}$'
union all
select
    b.thread_id,
    format('the messages of checkpoint %s are serialized as %s, and the migration reads msgpack only', l.checkpoint_id, b.type)
from
    last_checkpoint as l
    join :"checkpoints_schema".checkpoint_blobs as b
        on b.thread_id = l.thread_id
        and b.checkpoint_ns = l.checkpoint_ns
        and b.channel = 'messages'
        and b.version = l.messages_version
where
    b.type not in ('msgpack', 'empty')
union all
select
    w.thread_id,
    format('pending write %s/%s of checkpoint %s is serialized as %s, and the migration reads msgpack only', w.task_id, w.idx, l.checkpoint_id, w.type)
from
    last_checkpoint as l
    join :"checkpoints_schema".checkpoint_writes as w
        on w.thread_id = l.thread_id
        and w.checkpoint_ns = l.checkpoint_ns
        and w.checkpoint_id = l.checkpoint_id
        and w.channel = 'messages'
where
    w.type is distinct from 'msgpack';

select count(*) > 0 as refused from refused \gset
\if :refused
    \echo 'These threads cannot be migrated:'
    select thread_id, reason from refused order by thread_id;
    do $$ begin raise exception 'Some threads cannot be migrated (listed above), so nothing was changed. Fix or remove them in the checkpoints and run the migration again.'; end $$;
\endif

create temporary table kept_by_chat on commit drop as
select
    l.thread_id,
    t.meta ->> 'agent_session' as session_id
from
    last_checkpoint as l
    join :"chainlit_schema".threads as t
        on t.id::text = l.thread_id
where
    t.meta ->> 'agent_session' <> l.thread_id;

\echo 'Threads the chat already keeps in another agent session; they are not migrated:'
select thread_id, session_id from kept_by_chat order by thread_id;

-- 2. Рабочая схема и её функции.
--
-- Схема history_migrate живёт только внутри этой транзакции: в ней лежат
-- функции разбора, разобранные сообщения и собранные записи. В конце
-- переноса она удаляется. Если схема с таким именем уже есть, перенос
-- останавливается на её создании, чтобы не тронуть чужие объекты.

create schema history_migrate;

-- Функция msgpack_json разбирает значение, которое чекпойнтер langgraph
-- записал с type = 'msgpack', в jsonb. Разбор идёт одним проходом по байтам
-- без рекурсии: открытые массивы и словари лежат на стеке, а текст JSON
-- копится кусками в массиве и в конце превращается в jsonb одним
-- приведением. Сообщение langchain записано расширением msgpack с кодом 5
-- (модель pydantic), его тело — массив [модуль, класс, поля, метод], и
-- функция разворачивает расширение в этот массив. Другие расширения,
-- двоичные строки, NaN и нестроковые ключи словарей она отвергает с номером
-- байта, чтобы ничего не потерялось молча. Символ U+0000 jsonb не хранит,
-- поэтому в строках он заменяется на U+FFFD.
create function history_migrate.msgpack_json(blob bytea)
returns jsonb
language plpgsql
immutable
strict
as $$
declare
    buf     bytea := blob || '\x'::bytea;
    total   integer := length(buf);
    pos     integer := 0;
    parts   text[] := '{}';
    remain  integer[] := '{}';
    size    integer[] := '{}';
    is_map  boolean[] := '{}';
    depth   integer := 0;
    skip_separator boolean := false;
    b       integer;
    k       integer;
    i       integer;
    len     bigint;
    u       numeric;
    expo    integer;
    mant    numeric;
    piece   text;
    raw     bytea;
    nul     integer;
    opened  boolean;
    is_key  boolean;
begin
    loop
        if pos >= total then
            raise exception 'The msgpack value ends early at byte % of %.', pos, total;
        end if;

        is_key := false;
        if depth > 0 and not skip_separator then
            if is_map[depth] then
                is_key := remain[depth] % 2 = 0;
                if not is_key then
                    parts := array_append(parts, ':'::text);
                elsif remain[depth] < size[depth] then
                    parts := array_append(parts, ','::text);
                end if;
            elsif remain[depth] < size[depth] then
                parts := array_append(parts, ','::text);
            end if;
        end if;
        skip_separator := false;

        b := get_byte(buf, pos);
        pos := pos + 1;
        piece := null;
        opened := false;
        len := null;

        if b <= 127 then
            piece := b::text;
        elsif b >= 224 then
            piece := (b - 256)::text;
        elsif b between 128 and 159 or b in (220, 221, 222, 223) then
            if b between 128 and 159 then
                len := b & 15;
            else
                k := 2;
                if b in (221, 223) then
                    k := 4;
                end if;
                len := 0;
                for i in 0 .. k - 1 loop
                    len := len * 256 + get_byte(buf, pos + i);
                end loop;
                pos := pos + k;
            end if;

            if b between 128 and 143 or b in (222, 223) then
                if len = 0 then
                    piece := '{}';
                else
                    parts := array_append(parts, '{'::text);
                    depth := depth + 1;
                    remain[depth] := len * 2;
                    size[depth] := len * 2;
                    is_map[depth] := true;
                    opened := true;
                end if;
            else
                if len = 0 then
                    piece := '[]';
                else
                    parts := array_append(parts, '['::text);
                    depth := depth + 1;
                    remain[depth] := len;
                    size[depth] := len;
                    is_map[depth] := false;
                    opened := true;
                end if;
            end if;
        elsif b between 160 and 191 or b in (217, 218, 219) then
            if b between 160 and 191 then
                len := b & 31;
            else
                k := 1;
                if b = 218 then
                    k := 2;
                elsif b = 219 then
                    k := 4;
                end if;
                len := 0;
                for i in 0 .. k - 1 loop
                    len := len * 256 + get_byte(buf, pos + i);
                end loop;
                pos := pos + k;
            end if;

            raw := substring(buf from pos + 1 for len::integer);
            pos := pos + len::integer;
            nul := position('\x00'::bytea in raw);
            while nul > 0 loop
                raw := overlay(raw placing '\xefbfbd'::bytea from nul for 1);
                nul := position('\x00'::bytea in raw);
            end loop;
            piece := to_json(convert_from(raw, 'UTF8'))::text;
        elsif b = 192 then
            piece := 'null';
        elsif b = 194 then
            piece := 'false';
        elsif b = 195 then
            piece := 'true';
        elsif b between 204 and 211 then
            k := 1 << ((b - 204) % 4);
            u := 0;
            for i in 0 .. k - 1 loop
                u := u * 256 + get_byte(buf, pos + i);
            end loop;
            pos := pos + k;
            if b >= 208 and u >= 2::numeric ^ (8 * k - 1) then
                u := u - 2::numeric ^ (8 * k);
            end if;
            piece := trunc(u)::text;
        elsif b in (202, 203) then
            k := 4;
            if b = 203 then
                k := 8;
            end if;
            u := 0;
            for i in 0 .. k - 1 loop
                u := u * 256 + get_byte(buf, pos + i);
            end loop;
            pos := pos + k;
            if k = 8 then
                expo := (trunc(u / 2::numeric ^ 52) % 2048)::integer;
                mant := u % 2::numeric ^ 52;
                if expo = 2047 then
                    raise exception 'The msgpack value holds NaN or infinity at byte %, and JSON has no such number.', pos - 9;
                end if;
                if expo = 0 then
                    piece := to_json(mant::float8 * power(2::float8, -1074))::text;
                else
                    piece := to_json((mant + 2::numeric ^ 52)::float8 * power(2::float8, expo - 1075))::text;
                end if;
                if u >= 2::numeric ^ 63 then
                    piece := '-' || piece;
                end if;
            else
                expo := (trunc(u / 2::numeric ^ 23) % 256)::integer;
                mant := u % 2::numeric ^ 23;
                if expo = 255 then
                    raise exception 'The msgpack value holds NaN or infinity at byte %, and JSON has no such number.', pos - 5;
                end if;
                if expo = 0 then
                    piece := to_json((mant::float8 * power(2::float8, -149))::float4)::text;
                else
                    piece := to_json(((mant + 2::numeric ^ 23)::float8 * power(2::float8, expo - 150))::float4)::text;
                end if;
                if u >= 2::numeric ^ 31 then
                    piece := '-' || piece;
                end if;
            end if;
        elsif b in (199, 200, 201) then
            k := 1 << (b - 199);
            pos := pos + k;
            if get_byte(buf, pos) <> 5 then
                raise exception 'The msgpack extension % at byte % is not a pydantic model (5), and the chat history has no other extensions.', get_byte(buf, pos), pos;
            end if;
            pos := pos + 1;
            if is_key then
                raise exception 'The msgpack map at byte % uses an extension as a key.', pos;
            end if;
            skip_separator := true;
            continue;
        else
            raise exception 'The msgpack format byte 0x% at byte % is binary, fixext or reserved, and the chat history does not use it.', lpad(to_hex(b), 2, '0'), pos - 1;
        end if;

        if is_key and (piece is null or left(piece, 1) <> '"') then
            raise exception 'The msgpack map at byte % has a key that is not a string.', pos;
        end if;

        if opened then
            continue;
        end if;

        parts := array_append(parts, piece::text);

        loop
            if depth = 0 then
                if pos <> total then
                    raise exception 'The msgpack value is followed by % more byte(s).', total - pos;
                end if;
                return array_to_string(parts, '')::jsonb;
            end if;

            remain[depth] := remain[depth] - 1;
            exit when remain[depth] > 0;

            if is_map[depth] then
                parts := array_append(parts, '}'::text);
            else
                parts := array_append(parts, ']'::text);
            end if;
            depth := depth - 1;
        end loop;
    end loop;
end
$$;

-- Идентификатор записи, который не зависит от запуска: uuid из md5 строки
-- ключа с битами версии 3 и варианта RFC 4122. Ключ складывается из id
-- треда, номера сообщения и места записи, например «<тред>/12/0».
create function history_migrate.uuid_of(key text)
returns text
language sql
immutable
strict
as $$
    select (
        substr(h, 1, 12)
        || '3' || substr(h, 14, 3)
        || to_hex((('x' || substr(h, 17, 1))::bit(4)::integer & 3) | 8)
        || substr(h, 18, 15)
    )::uuid::text
    from (select md5(key) as h) as digest
$$;

-- Строка из 24 знаков для идентификаторов запроса и ответа вида req_… и
-- msg_…, которые агент выдаёт сам; она тоже берётся из md5 ключа.
create function history_migrate.token_of(key text)
returns text
language sql
immutable
strict
as $$
    select left(md5(key), 24)
$$;

-- Имя файла как сегмент пути URL: буквы, цифры и знаки _ . - ~ остаются,
-- остальные байты UTF-8 пишутся как %XX. Так чат кодирует имя в ссылке на
-- файл, например «отчёт 1.pdf» становится %D0%BE…%201.pdf.
create function history_migrate.url_segment(name text)
returns text
language sql
immutable
strict
as $$
    select coalesce(string_agg(
        case
            when ch ~ '^[A-Za-z0-9_.~-]$' then ch
            else upper(regexp_replace(encode(convert_to(ch, 'UTF8'), 'hex'), '(..)', '%\1', 'g'))
        end,
        '' order by n
    ), '')
    from regexp_split_to_table(name, '') with ordinality as chars(ch, n)
$$;

-- Вид файла вложения, у строки elements которого вида не было: по суффиксу
-- имени, неизвестный суффикс — application/octet-stream. Ссылка нового чата
-- всегда называет вид, поэтому перенесённая ссылка получает его здесь.
create function history_migrate.mime_of(name text)
returns text
language sql
immutable
strict
as $$
    select coalesce(
        (
            select
                known.mime
            from (
                values
                    ('pdf', 'application/pdf'),
                    ('png', 'image/png'),
                    ('jpg', 'image/jpeg'),
                    ('jpeg', 'image/jpeg'),
                    ('gif', 'image/gif'),
                    ('svg', 'image/svg+xml'),
                    ('webp', 'image/webp'),
                    ('bmp', 'image/bmp'),
                    ('txt', 'text/plain'),
                    ('log', 'text/plain'),
                    ('md', 'text/markdown'),
                    ('mmd', 'text/vnd.mermaid'),
                    ('csv', 'text/csv'),
                    ('tsv', 'text/tab-separated-values'),
                    ('json', 'application/json'),
                    ('xml', 'application/xml'),
                    ('html', 'text/html'),
                    ('htm', 'text/html'),
                    ('sql', 'application/sql'),
                    ('zip', 'application/zip'),
                    ('gz', 'application/gzip'),
                    ('tar', 'application/x-tar'),
                    ('mp4', 'video/mp4'),
                    ('webm', 'video/webm'),
                    ('mov', 'video/quicktime'),
                    ('mp3', 'audio/mpeg'),
                    ('wav', 'audio/wav'),
                    ('ogg', 'audio/ogg'),
                    ('m4a', 'audio/mp4'),
                    ('flac', 'audio/flac'),
                    ('doc', 'application/msword'),
                    ('docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                    ('xls', 'application/vnd.ms-excel'),
                    ('xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
                    ('ppt', 'application/vnd.ms-powerpoint'),
                    ('pptx', 'application/vnd.openxmlformats-officedocument.presentationml.presentation')
            ) as known(suffix, mime)
            where
                known.suffix = lower(substring(name from '\.([^./]+)$'))
        ),
        'application/octet-stream'
    )
$$;

-- Истинность значения поля сообщения так, как её понимает условие if в
-- Python: null, false, 0, пустая строка, пустой массив и пустой словарь
-- ложны, остальное истинно.
create function history_migrate.truthy(value jsonb)
returns boolean
language sql
immutable
as $$
    select value is not null
        and value not in ('null'::jsonb, 'false'::jsonb, '0'::jsonb, '""'::jsonb, '[]'::jsonb, '{}'::jsonb)
$$;

-- Текст сообщения langchain: строка как есть, а у списка блоков — склейка
-- строк и текстовых блоков в их порядке.
create function history_migrate.message_text(content jsonb)
returns text
language sql
immutable
as $$
    select case
        when jsonb_typeof(content) = 'string' then content #>> '{}'
        when jsonb_typeof(content) = 'array' then (
            select
                coalesce(string_agg(
                    case
                        when jsonb_typeof(block) = 'string' then block #>> '{}'
                        when block ->> 'type' = 'text' then coalesce(block ->> 'text', '')
                    end,
                    '' order by ord
                ), '')
            from
                jsonb_array_elements(content) with ordinality as blocks(block, ord)
        )
        else ''
    end
$$;

-- Адрес журнала вызова из response_metadata.boba_journal сообщения dev:
-- годится только целый — сервер, запуск и идентификатор вызова строками.
-- Адрес без идентификатора вызова (его писали промежуточные выпуски) по
-- журналу сервера не находится, поэтому не переносится: шаг вызова
-- остаётся без кнопки журнала, а заметка перевода называет такой итог.
create function history_migrate.journal(address jsonb)
returns jsonb
language sql
immutable
as $$
    select case
        when jsonb_typeof(address) = 'object'
            and (select array_agg(key order by key) from jsonb_object_keys(address) as keys(key)) = array['call', 'run', 'server']
            and jsonb_typeof(address -> 'server') = 'string' and address ->> 'server' <> ''
            and jsonb_typeof(address -> 'run') = 'string' and address ->> 'run' <> ''
            and jsonb_typeof(address -> 'call') = 'string' and address ->> 'call' <> ''
        then address
    end
$$;

-- Ссылки на каналы журнала вызова в итоге: по блоку resource_link на каналы
-- tool_stdout и tool_stderr, как их дописывает к результату tools/call сервер
-- boba-mcp по расширению журнала — uri boba-journal://{run}/{node}/{channel},
-- имя блока — канал, mimeType text/plain, блок адресован человеку (audience
-- user). Адрес не целый — ссылок нет.
create function history_migrate.journal_links(address jsonb)
returns jsonb
language sql
immutable
as $$
    select coalesce(
        (
            select
                jsonb_agg(
                    jsonb_build_object(
                        'type', 'resource_link',
                        'uri', 'boba-journal://'
                            || history_migrate.url_segment(history_migrate.journal(address) ->> 'run') || '/'
                            || history_migrate.url_segment(history_migrate.journal(address) ->> 'call') || '/'
                            || channel,
                        'name', channel,
                        'mimeType', 'text/plain',
                        'annotations', jsonb_build_object('audience', jsonb_build_array('user'))
                    )
                    order by ord
                )
            from
                unnest(array['tool_stdout', 'tool_stderr']) with ordinality as channels(channel, ord)
            where
                history_migrate.journal(address) is not null
        ),
        '[]'::jsonb
    )
$$;

-- SHA-1 значения байтов: uuid версии 5 строится на нём, а встроенного
-- sha1 у Postgres нет (есть только в расширении pgcrypto). Реализация
-- прямая по FIPS 180-4: дополнение до кратного 64 байтам, 80 раундов на
-- блок, 32-битная арифметика в bigint с маской.
create function history_migrate.sha1(data bytea)
returns bytea
language plpgsql
immutable
strict
as $$
declare
    mask   constant bigint := 4294967295;
    padded bytea := data || '\x80'::bytea;
    h0     bigint := 1732584193;
    h1     bigint := 4023233417;
    h2     bigint := 2562383102;
    h3     bigint := 271733878;
    h4     bigint := 3285377520;
    w      bigint[];
    a      bigint;
    b      bigint;
    c      bigint;
    d      bigint;
    e      bigint;
    f      bigint;
    k      bigint;
    t      bigint;
    base   integer;
    i      integer;
begin
    while length(padded) % 64 <> 56 loop
        padded := padded || '\x00'::bytea;
    end loop;
    padded := padded || decode(lpad(to_hex(length(data)::bigint * 8), 16, '0'), 'hex');

    for block in 0 .. length(padded) / 64 - 1 loop
        base := block * 64;
        w := array_fill(0::bigint, array[80]);
        for i in 0 .. 15 loop
            w[i + 1] := (get_byte(padded, base + i * 4)::bigint << 24)
                | (get_byte(padded, base + i * 4 + 1)::bigint << 16)
                | (get_byte(padded, base + i * 4 + 2)::bigint << 8)
                | get_byte(padded, base + i * 4 + 3)::bigint;
        end loop;
        for i in 16 .. 79 loop
            t := w[i - 2] # w[i - 7] # w[i - 13] # w[i - 15];
            w[i + 1] := ((t << 1) | (t >> 31)) & mask;
        end loop;

        a := h0;
        b := h1;
        c := h2;
        d := h3;
        e := h4;
        for i in 0 .. 79 loop
            if i < 20 then
                f := (b & c) | ((~b & mask) & d);
                k := 1518500249;
            elsif i < 40 then
                f := b # c # d;
                k := 1859775393;
            elsif i < 60 then
                f := (b & c) | (b & d) | (c & d);
                k := 2400959708;
            else
                f := b # c # d;
                k := 3395469782;
            end if;
            t := ((((a << 5) | (a >> 27)) & mask) + f + e + k + w[i + 1]) & mask;
            e := d;
            d := c;
            c := ((b << 30) | (b >> 2)) & mask;
            b := a;
            a := t;
        end loop;

        h0 := (h0 + a) & mask;
        h1 := (h1 + b) & mask;
        h2 := (h2 + c) & mask;
        h3 := (h3 + d) & mask;
        h4 := (h4 + e) & mask;
    end loop;

    return decode(
        lpad(to_hex(h0), 8, '0') || lpad(to_hex(h1), 8, '0') || lpad(to_hex(h2), 8, '0')
            || lpad(to_hex(h3), 8, '0') || lpad(to_hex(h4), 8, '0'),
        'hex'
    );
end
$$;

-- uuid версии 5 (RFC 4122): SHA-1 от байтов пространства и имени в UTF-8,
-- первые 16 байт с битами версии 5 и варианта; то же, что uuid.uuid5 Python.
create function history_migrate.uuid5(namespace uuid, name text)
returns uuid
language sql
immutable
strict
as $$
    select encode(
        set_byte(
            set_byte(substring(h from 1 for 16), 6, (get_byte(h, 6) & 15) | 80),
            8,
            (get_byte(h, 8) & 63) | 128
        ),
        'hex'
    )::uuid
    from (
        select history_migrate.sha1(decode(replace(namespace::text, '-', ''), 'hex') || convert_to(name, 'UTF8')) as h
    ) as digest
$$;

-- Id шага ответа ленты: ChatView.derive_id(тред, ключ хода[#номер], answer)
-- нового чата, то же выводила и лента старого. Ключ хода — id вопроса, номер
-- — порядок ответа в ходе с нуля, нулевой пишется без «#0»; пространство —
-- ChatView.NAMESPACE.
create function history_migrate.answer_id(thread_id text, turn_key text, number bigint)
returns uuid
language sql
immutable
strict
as $$
    select history_migrate.uuid5(
        '6f9b1f4e-2f1a-4c1a-9a2f-1d3b5c7e9a11',
        thread_id || '/' || turn_key
            || case when number > 0 then '#' || number else '' end
            || '/answer'
    )
$$;

-- 3. Извлечение.
--
-- Каждая строка таблицы source_messages — одно сообщение langchain треда:
-- его класс, поля модели и номер в треде. Сначала идут сообщения канала,
-- затем ожидающие записи по task_id и idx. Ожидающие записи несут итоги
-- инструментов хода, оборванного до конца узла, и сообщение из них берётся,
-- только если сообщения с тем же id в канале ещё нет.

create table history_migrate.source_messages (
    thread_id text    not null,
    n         integer not null,
    stamp     text    not null,
    class     text    not null,
    message   jsonb   not null,
    primary key (thread_id, n)
);

insert into history_migrate.source_messages (thread_id, n, stamp, class, message)
with migrated as (
    select
        l.*
    from
        last_checkpoint as l
    where
        not exists (select 1 from kept_by_chat as k where k.thread_id = l.thread_id)
),
stored as (
    select
        l.thread_id,
        l.stamp,
        0 as part,
        '' as task_id,
        0 as idx,
        item.ord,
        item.value
    from
        migrated as l
        join :"checkpoints_schema".checkpoint_blobs as b
            on b.thread_id = l.thread_id
            and b.checkpoint_ns = l.checkpoint_ns
            and b.channel = 'messages'
            and b.version = l.messages_version
            and b.type = 'msgpack'
        cross join lateral jsonb_array_elements(history_migrate.msgpack_json(b.blob))
            with ordinality as item(value, ord)
),
written as (
    select
        l.thread_id,
        l.stamp,
        1 as part,
        w.task_id,
        w.idx,
        history_migrate.msgpack_json(w.blob) as value
    from
        migrated as l
        join :"checkpoints_schema".checkpoint_writes as w
            on w.thread_id = l.thread_id
            and w.checkpoint_ns = l.checkpoint_ns
            and w.checkpoint_id = l.checkpoint_id
            and w.channel = 'messages'
),
pending as (
    select
        thread_id,
        stamp,
        part,
        task_id,
        idx,
        item.ord,
        item.value
    from
        written
        cross join lateral jsonb_array_elements(
            case
                when jsonb_typeof(written.value -> 0) = 'string' then jsonb_build_array(written.value)
                else written.value
            end
        ) with ordinality as item(value, ord)
),
messages as (
    select * from stored
    union all
    select
        p.*
    from
        pending as p
    where
        not exists (
            select
                1
            from
                stored as s
            where
                s.thread_id = p.thread_id
                and s.value -> 2 ->> 'id' = p.value -> 2 ->> 'id'
        )
)
select
    thread_id,
    row_number() over (partition by thread_id order by part, task_id, idx, ord) as n,
    stamp,
    value ->> 1 as class,
    value -> 2 as message
from
    messages;

-- 4. Перевод.
--
-- Тред становится сессией агента с тем же id, записи идут по seq с 1 и
-- связаны цепочкой parentUuid. Вопрос человека даёт запись user с origin
-- human, и её uuid равен id сообщения, потому что по нему чат привязывает
-- оценки. Ответ модели даёт по записи assistant на каждый блок (рассуждение,
-- текст, вызов инструмента) с общими message.id и requestId; модель берётся
-- последняя названная в треде, а у подготовки хода стоит <synthetic>. Ответ
-- с пометкой error становится синтетической записью об ошибке модели, ответ
-- с пометкой stopped — оборванным ответом и маркером прерывания за ним.
-- Итог инструмента даёт запись user с блоком tool_result и toolUseResult —
-- результатом tools/call в форме MCP, как его пишет агент: текст итога
-- одним блоком text, ссылки resource_link на каналы журнала вызова по его
-- адресу, isError по статусу. Артефакт результата dev (модель семейства
-- boba) и типы содержимого аргументов не переносятся: лента рисует итог по
-- блокам, аргументы — обычным JSON. Время всех
-- записей треда — время его последнего checkpoint'а, потому что своего
-- времени у сообщений нет. Ключи со значением null в документ не пишутся,
-- кроме parentUuid и stop_sequence: так записи пишет сам агент.
--
-- Вложение вопроса чат записывал в additional_kwargs.attachments именем и
-- путём файла в песочнице. Маршрут canvas чата отдаёт файл workspace по
-- треду, каталогу (upload или mermaid) и имени, поэтому ссылку получает
-- только файл, путь которого кончается на <id этого треда>/upload/<имя> или
-- <id этого треда>/mermaid/<имя>: она ведёт на <chat_url>/canvas/ с тем же
-- местом и видом файла запросом ?mime=, как ссылка нового чата, а mimeType
-- берётся из строки elements этого вопроса; вида нет и там — он берётся по
-- суффиксу имени (history_migrate.mime_of). Файл вне
-- каталогов треда, например /workspace/upload/<id> старой раскладки,
-- маршрут не отдаст, поэтому ссылки у него нет: его имя остаётся в тексте
-- вопроса в виде [@имя].

create table history_migrate.units (
    thread_id    text    not null,
    n            integer not null,
    sub          integer not null,
    type         text    not null,
    uuid         text    not null,
    call_id      text,
    answers_call text,
    body         jsonb   not null,
    primary key (thread_id, n, sub)
);

insert into history_migrate.units
with message as (
    select
        s.thread_id,
        s.n,
        s.class,
        s.message as m,
        coalesce(s.message -> 'additional_kwargs', '{}'::jsonb) as extra,
        history_migrate.message_text(s.message -> 'content') as body_text
    from
        history_migrate.source_messages as s
),
reply as (
    select
        message.*,
        coalesce(nullif(m ->> 'id', ''), 'msg_' || history_migrate.token_of(thread_id || '/' || n || '/message')) as message_id,
        'req_' || history_migrate.token_of(thread_id || '/' || n || '/request') as request_id,
        jsonb_build_object(
            'input_tokens', coalesce((m -> 'usage_metadata' ->> 'input_tokens')::integer, 0),
            'output_tokens', coalesce((m -> 'usage_metadata' ->> 'output_tokens')::integer, 0),
            'cache_creation_input_tokens', 0,
            'cache_read_input_tokens', 0
        ) as usage,
        case
            when history_migrate.truthy(extra -> 'error') then 'error'
            when history_migrate.truthy(extra -> 'stopped') then 'stopped'
            else 'reply'
        end as shape
    from
        message
    where
        class = 'AIMessage'
),
block as (
    select
        r.thread_id,
        r.n,
        0 as slot,
        jsonb_build_object('type', 'thinking', 'thinking', r.extra ->> 'reasoning_content', 'signature', '') as content,
        null::text as call_id
    from
        reply as r
    where
        r.shape <> 'error'
        and history_migrate.truthy(r.extra -> 'reasoning_content')
    union all
    select
        r.thread_id,
        r.n,
        1,
        jsonb_build_object('type', 'text', 'text', r.body_text),
        null
    from
        reply as r
    where
        r.shape <> 'error'
        and r.body_text <> ''
    union all
    select
        r.thread_id,
        r.n,
        1 + call.ord::integer,
        jsonb_build_object(
            'type', 'tool_use',
            'id', call.value ->> 'id',
            'name', call.value ->> 'name',
            'input', jsonb_strip_nulls(
                case
                    when jsonb_typeof(call.value -> 'args') = 'object' then call.value -> 'args'
                    else '{}'::jsonb
                end
            )
        ),
        call.value ->> 'id'
    from
        reply as r
        cross join lateral jsonb_array_elements(coalesce(r.m -> 'tool_calls', '[]'::jsonb))
            with ordinality as call(value, ord)
    where
        r.shape <> 'error'
        and history_migrate.truthy(call.value -> 'id')
        and history_migrate.truthy(call.value -> 'name')
),
indexed_block as (
    select
        block.*,
        (row_number() over (partition by thread_id, n order by slot) - 1)::integer as api_block_index
    from
        block
),
named_model as (
    select
        r.thread_id,
        r.n,
        nullif(r.m -> 'response_metadata' ->> 'model_name', '') as model_name
    from
        reply as r
    where
        r.shape = 'reply'
        and not (r.extra ? 'prefetch_elapsed_ms')
        and exists (select 1 from block as b where b.thread_id = r.thread_id and b.n = r.n)
),
carried_model as (
    select
        thread_id,
        n,
        coalesce(
            first_value(model_name) over (partition by thread_id, seen order by n),
            '<unknown>'
        ) as model
    from (
        select
            named_model.*,
            count(model_name) over (partition by thread_id order by n) as seen
        from
            named_model
    ) as counted
),
reply_unit as (
    select
        r.thread_id,
        r.n,
        b.api_block_index as sub,
        'assistant' as type,
        history_migrate.uuid_of(r.thread_id || '/' || r.n || '/' || b.api_block_index) as uuid,
        b.call_id,
        null::text as answers_call,
        jsonb_build_object(
            'type', 'assistant',
            'message', jsonb_build_object(
                'id', r.message_id,
                'type', 'message',
                'role', 'assistant',
                'model', case
                    when r.shape = 'stopped' then '<synthetic>'
                    when r.extra ? 'prefetch_elapsed_ms' then '<synthetic>'
                    else cm.model
                end,
                'content', jsonb_build_array(b.content),
                'stop_reason', case
                    when r.shape = 'reply' and jsonb_array_length(coalesce(r.m -> 'tool_calls', '[]'::jsonb)) > 0 then 'tool_use'
                    else 'end_turn'
                end,
                'stop_sequence', null,
                'usage', r.usage
            ),
            'apiBlockIndex', b.api_block_index,
            'requestId', r.request_id
        ) || case
            when r.shape = 'stopped' then '{"isAbortedMidStream": true}'::jsonb
            else '{}'::jsonb
        end as body
    from
        reply as r
        join indexed_block as b
            on b.thread_id = r.thread_id
            and b.n = r.n
        left join carried_model as cm
            on cm.thread_id = r.thread_id
            and cm.n = r.n
    where
        r.shape <> 'error'
),
failure_unit as (
    select
        r.thread_id,
        r.n,
        0 as sub,
        'assistant' as type,
        history_migrate.uuid_of(r.thread_id || '/' || r.n || '/error') as uuid,
        null::text as call_id,
        null::text as answers_call,
        jsonb_build_object(
            'type', 'assistant',
            'message', jsonb_build_object(
                'id', r.message_id,
                'type', 'message',
                'role', 'assistant',
                'model', '<synthetic>',
                'content', jsonb_build_array(jsonb_build_object('type', 'text', 'text', r.body_text)),
                'stop_reason', 'stop_sequence',
                'stop_sequence', null,
                'usage', '{"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}'::jsonb
            ),
            'apiBlockIndex', 0,
            'requestId', r.request_id,
            'isApiErrorMessage', true,
            'error', r.body_text
        ) as body
    from
        reply as r
    where
        r.shape = 'error'
),
interrupt_unit as (
    select
        r.thread_id,
        r.n,
        1000 as sub,
        'user' as type,
        history_migrate.uuid_of(r.thread_id || '/' || r.n || '/interrupted') as uuid,
        null::text as call_id,
        null::text as answers_call,
        jsonb_build_object(
            'type', 'user',
            'message', jsonb_build_object('role', 'user', 'content', '[Request interrupted by user]')
        ) as body
    from
        reply as r
    where
        r.shape = 'stopped'
),
attached as (
    select
        q.thread_id,
        q.n,
        item.ord,
        item.value ->> 'name' as name,
        regexp_match(item.value ->> 'path', '/([^/]+)/(upload|mermaid)/([^/]+)$') as place,
        coalesce(
            (
                select
                    nullif(e.mime, '')
                from
                    :"chainlit_schema".elements as e
                where
                    e.for_id::text = q.m ->> 'id'
                    and e.name = item.value ->> 'name'
                order by
                    e.id
                limit 1
            ),
            history_migrate.mime_of(item.value ->> 'name')
        ) as mime
    from
        message as q
        cross join lateral jsonb_array_elements(
            case
                when jsonb_typeof(q.extra -> 'attachments') = 'array' then q.extra -> 'attachments'
                else '[]'::jsonb
            end
        ) with ordinality as item(value, ord)
    where
        q.class = 'HumanMessage'
        and jsonb_typeof(item.value) = 'object'
        and item.value ->> 'name' is not null
        and item.value ->> 'path' is not null
),
question_attachments as (
    select
        a.thread_id,
        a.n,
        jsonb_agg(
            jsonb_build_object(
                'type', 'resource_link',
                'name', a.name,
                'uri', rtrim(:'chat_url', '/') || '/canvas/' || a.thread_id || '/' || a.place[2] || '/'
                    || history_migrate.url_segment(a.place[3])
                    || '?mime=' || history_migrate.url_segment(a.mime),
                'mimeType', a.mime
            )
            order by a.ord
        ) filter (where a.place[1] = a.thread_id) as links,
        string_agg('[@' || a.name || ']', '' order by a.ord) filter (where a.place[1] is distinct from a.thread_id) as unlinked
    from
        attached as a
    group by
        a.thread_id,
        a.n
),
question as (
    select
        q.*,
        q.body_text || coalesce(qa.unlinked, '') as text,
        qa.links
    from
        message as q
        left join question_attachments as qa
            on qa.thread_id = q.thread_id
            and qa.n = q.n
    where
        q.class = 'HumanMessage'
),
question_unit as (
    select
        q.thread_id,
        q.n,
        0 as sub,
        'user' as type,
        case
            when nullif(q.m ->> 'id', '') is null then history_migrate.uuid_of(q.thread_id || '/' || q.n || '/question')
            when row_number() over (partition by q.thread_id, q.m ->> 'id' order by q.n) > 1 then history_migrate.uuid_of(q.thread_id || '/' || q.n || '/question')
            else q.m ->> 'id'
        end as uuid,
        null::text as call_id,
        null::text as answers_call,
        jsonb_build_object(
            'type', 'user',
            'message', jsonb_build_object(
                'role', 'user',
                'content', case
                    when q.links is null then to_jsonb(q.text)
                    else jsonb_build_array(jsonb_build_object('type', 'text', 'text', q.text)) || q.links
                end
            ),
            'origin', '{"kind": "human"}'::jsonb
        ) as body
    from
        question as q
),
tool_unit as (
    select
        t.thread_id,
        t.n,
        0 as sub,
        'user' as type,
        history_migrate.uuid_of(t.thread_id || '/' || t.n || '/result') as uuid,
        null::text as call_id,
        t.m ->> 'tool_call_id' as answers_call,
        jsonb_build_object(
            'type', 'user',
            'message', jsonb_build_object(
                'role', 'user',
                'content', jsonb_build_array(
                    jsonb_build_object(
                        'type', 'tool_result',
                        'tool_use_id', t.m ->> 'tool_call_id',
                        'content', t.body_text
                    ) || case
                        when t.m ->> 'status' = 'error' then '{"is_error": true}'::jsonb
                        else '{}'::jsonb
                    end
                )
            )
        ) || jsonb_build_object(
            'toolUseResult', jsonb_build_object(
                'content', jsonb_build_array(jsonb_build_object('type', 'text', 'text', t.body_text))
                    || history_migrate.journal_links(t.m -> 'response_metadata' -> 'boba_journal'),
                'isError', coalesce(t.m ->> 'status' = 'error', false)
            )
        ) as body
    from
        message as t
    where
        t.class = 'ToolMessage'
)
select * from question_unit
union all
select * from reply_unit
union all
select * from failure_unit
union all
select * from interrupt_unit
union all
select * from tool_unit;

-- Записи собираются сразу в раскладке таблицы session_records: сессия, seq, вид и
-- время — колонками, остальное — документом record без этих четырёх ключей.
create table history_migrate.records (
    session_id  text        not null,
    seq         integer     not null,
    type        text        not null,
    recorded_at timestamptz not null,
    record      jsonb       not null,
    primary key (session_id, seq)
);

insert into history_migrate.records (session_id, seq, type, recorded_at, record)
with ordered as (
    select
        u.*,
        row_number() over (partition by thread_id order by n, sub)::integer as seq,
        lag(uuid) over (partition by thread_id order by n, sub) as parent_uuid
    from
        history_migrate.units as u
),
sourced as (
    select
        o.*,
        (
            select
                c.uuid
            from
                ordered as c
            where
                c.thread_id = o.thread_id
                and c.call_id = o.answers_call
                and c.seq < o.seq
            order by
                c.seq desc
            limit 1
        ) as source_uuid
    from
        ordered as o
),
stamped as (
    select distinct on (thread_id)
        thread_id,
        date_trunc('milliseconds', stamp::timestamptz) as ts
    from
        history_migrate.source_messages
    order by
        thread_id
)
select
    s.thread_id,
    s.seq,
    s.type,
    m.ts,
    jsonb_build_object(
        'uuid', s.uuid,
        'parentUuid', s.parent_uuid,
        'isSidechain', false,
        'version', :'version'
    )
    || (s.body - 'type')
    || case
        when s.source_uuid is not null then jsonb_build_object('sourceToolAssistantUUID', s.source_uuid)
        else '{}'::jsonb
    end
from
    sourced as s
    join stamped as m
        on m.thread_id = s.thread_id;

-- Заметки перевода: сообщения, которые не стали записями, и вложения без
-- ссылки. Перенос они не останавливают, их выводит сверка.
create table history_migrate.notes (
    thread_id text not null,
    n         integer not null,
    note      text not null
);

insert into history_migrate.notes (thread_id, n, note)
select
    s.thread_id,
    s.n,
    format('Message %s of class %s has no record kind and is skipped.', s.message ->> 'id', s.class)
from
    history_migrate.source_messages as s
where
    s.class not in ('HumanMessage', 'AIMessage', 'ToolMessage')
union all
select
    s.thread_id,
    s.n,
    format('Reply %s has no text, reasoning or calls and is skipped.', s.message ->> 'id')
from
    history_migrate.source_messages as s
where
    s.class = 'AIMessage'
    and not exists (select 1 from history_migrate.units as u where u.thread_id = s.thread_id and u.n = s.n)
union all
select
    s.thread_id,
    s.n,
    format('Tool call %s of reply %s lacks an id or a name and is dropped.', call.value, s.message ->> 'id')
from
    history_migrate.source_messages as s
    cross join lateral jsonb_array_elements(coalesce(s.message -> 'tool_calls', '[]'::jsonb)) as call(value)
where
    s.class = 'AIMessage'
    and not (history_migrate.truthy(call.value -> 'id') and history_migrate.truthy(call.value -> 'name'))
union all
select
    s.thread_id,
    s.n,
    case
        when jsonb_typeof(item.value) <> 'object' or item.value ->> 'name' is null or item.value ->> 'path' is null then
            format('Attachment entry %s of question %s lacks a name or a path and is dropped.', item.value, s.message ->> 'id')
        else
            format('Attachment %s of question %s is not in a directory of its thread, so the chat cannot serve it; the question keeps its name as text without a link.', item.value ->> 'path', s.message ->> 'id')
    end
from
    history_migrate.source_messages as s
    cross join lateral jsonb_array_elements(
        case
            when jsonb_typeof(s.message -> 'additional_kwargs' -> 'attachments') = 'array' then s.message -> 'additional_kwargs' -> 'attachments'
            else '[]'::jsonb
        end
    ) as item(value)
where
    s.class = 'HumanMessage'
    and (
        jsonb_typeof(item.value) <> 'object'
        or item.value ->> 'name' is null
        or item.value ->> 'path' is null
        or (regexp_match(item.value ->> 'path', '/([^/]+)/(upload|mermaid)/[^/]+$'))[1] is distinct from s.thread_id
    )
union all
select
    s.thread_id,
    s.n,
    format('The journal address %s of tool result %s lacks the server, the run or the call, so the result keeps no journal link.', s.message -> 'response_metadata' -> 'boba_journal', s.message ->> 'tool_call_id')
from
    history_migrate.source_messages as s
where
    s.class = 'ToolMessage'
    and s.message -> 'response_metadata' -> 'boba_journal' is not null
    and s.message -> 'response_metadata' -> 'boba_journal' <> 'null'::jsonb
    and history_migrate.journal(s.message -> 'response_metadata' -> 'boba_journal') is null
union all
select
    r.session_id,
    r.seq,
    format('Tool result for call %s has no record of its call before it.', r.record -> 'message' -> 'content' -> 0 ->> 'tool_use_id')
from
    history_migrate.records as r
where
    r.record -> 'message' -> 'content' -> 0 ->> 'type' = 'tool_result'
    and not (r.record ? 'sourceToolAssistantUUID');

-- 5. Загрузка.
--
-- Таблицы истории (session и session_records) создаёт хранилище агента
-- (PostgresHistoryStore) при первом обращении, а перенос пишет в них раньше,
-- поэтому создаёт их сам теми же определениями из history_tables.sql рядом;
-- совпадение определений с кодом хранилища проверяет тест
-- boba-history-postgres. Комментарии к таблицам агент допишет сам при первом
-- обращении.
--
-- Если агент уже вёл историю в старой базе, её схема сначала
-- переименовывается в схему агента. Агент прежнего выпуска держал всю запись
-- одной таблицей history_records, документом вместе с сессией, seq, видом и
-- временем; её строки раскладываются по session и session_records так же, как пишет
-- хранилище, а сама таблица удаляется, когда число строк сошлось. Строка,
-- у которой ключи документа расходятся с колонками или нет времени,
-- останавливает перенос: при раскладке часть записи пропала бы. Беседа
-- треда переносится только в сессию, которой в истории ещё нет: сессию,
-- которую агент уже вёл, перенос не трогает.

select
    format('alter schema %I rename to %I', :'history_schema', :'boba_schema')
where
    :'history_schema' <> :'boba_schema'
    and to_regnamespace(:'history_schema') is not null
\gexec

create schema if not exists :"boba_schema";

set local search_path to :"boba_schema";
\ir history_tables.sql
reset search_path;

select to_regclass(format('%I.history_records', :'boba_schema')) is not null as has_old_history \gset
\if :has_old_history
select
    count(*) as old_history_inconsistent
from
    :"boba_schema".history_records
where
    record ->> 'sessionId' is distinct from session_id
    or record ->> 'seq' is distinct from seq::text
    or record ->> 'type' is distinct from type
    or record ->> 'timestamp' is null
\gset
select :old_history_inconsistent > 0 as old_history_broken \gset
\if :old_history_broken
    \echo 'Rows of the old agent history table history_records whose document keys disagree with their columns or lack a timestamp:' :old_history_inconsistent
    do $$ begin raise exception 'The old agent history table history_records cannot be split without losing data (count above), so nothing was changed.'; end $$;
\endif

insert into :"boba_schema".session (id, created_at)
select
    session_id,
    min((record ->> 'timestamp')::timestamptz)
from
    :"boba_schema".history_records
group by
    session_id;

insert into :"boba_schema".session_records (session_id, seq, type, recorded_at, record)
select
    session_id,
    seq,
    type,
    (record ->> 'timestamp')::timestamptz,
    record - array['type', 'seq', 'sessionId', 'timestamp']
from
    :"boba_schema".history_records;

select
    (select count(*) from :"boba_schema".history_records) as old_history_records,
    (
        select
            count(*)
        from
            :"boba_schema".session_records as h
        where
            exists (select 1 from :"boba_schema".history_records as o where o.session_id = h.session_id)
    ) as old_history_converted
\gset
\echo 'Records of the old agent history table history_records:' :old_history_records ', converted:' :old_history_converted
select :old_history_records <> :old_history_converted as old_history_lost \gset
\if :old_history_lost
    do $$ begin raise exception 'Converting the old agent history table history_records lost records (counts above), so nothing was changed.'; end $$;
\endif

drop table :"boba_schema".history_records;
\endif

create table history_migrate.loaded (
    session_id text    primary key,
    records    integer not null
);

insert into history_migrate.loaded (session_id, records)
select
    r.session_id,
    count(*)
from
    history_migrate.records as r
where
    not exists (
        select
            1
        from
            :"boba_schema".session as s
        where
            s.id = r.session_id
    )
group by
    r.session_id;

insert into :"boba_schema".session (id, created_at)
select
    r.session_id,
    min(r.recorded_at)
from
    history_migrate.records as r
    join history_migrate.loaded as l
        on l.session_id = r.session_id
group by
    r.session_id;

insert into :"boba_schema".session_records (session_id, seq, type, recorded_at, record)
select
    r.session_id,
    r.seq,
    r.type,
    r.recorded_at,
    r.record
from
    history_migrate.records as r
    join history_migrate.loaded as l
        on l.session_id = r.session_id
order by
    r.session_id,
    r.seq;

-- 6. Привязка тредов.
--
-- Чат находит сессию агента треда только по ключу agent_session в
-- threads.meta. Каждому переведённому треду, у которого есть строка в
-- threads, ключ записывается явно, со значением id самого треда. Тред без
-- строки в threads чат не покажет, но его сессия всё равно переносится.

create table history_migrate.bound (
    thread_id uuid primary key
);

with changed as (
    update
        :"chainlit_schema".threads as t
    set
        meta = case
            when jsonb_typeof(t.meta) = 'object' then t.meta
            else '{}'::jsonb
        end || jsonb_build_object('agent_session', t.id::text)
    where
        t.id::text in (select distinct thread_id from history_migrate.source_messages)
        and t.meta ->> 'agent_session' is distinct from t.id::text
    returning
        t.id
)
insert into history_migrate.bound (thread_id)
select id from changed;

-- 7. Оценки.
--
-- Оценка (feedbacks.for_id) привязана к id шага ответа. Новый чат выводит
-- этот id так же, как лента старого: из ключа хода и номера ответа в ходе
-- (answer_id). Ключ хода у обоих — id вопроса: перенос оставляет записи
-- вопроса id его сообщения. Номер считается по-разному: старая лента давала
-- номер каждому сообщению модели с текстом и каждому сообщению с ошибкой
-- модели, а новый чат — только тем сообщениям с текстом, которые агент
-- воспроизводит, то есть без синтетических записей (ошибка модели,
-- подготовка хода). Поэтому после ответа с ошибкой или подготовки номера
-- расходятся, и оценка такого ответа переводится на новый id. Оценка, которой
-- в новом чате нечего показать (её ответа нет в беседе треда), остаётся как
-- была и выводится заметкой.

create table history_migrate.answers (
    thread_id text    not null,
    n         integer not null,
    old_id    uuid,
    new_id    uuid,
    primary key (thread_id, n)
);

insert into history_migrate.answers
with turn as (
    select
        s.thread_id,
        s.n,
        s.class,
        s.message,
        max(s.n) filter (where s.class = 'HumanMessage') over (partition by s.thread_id order by s.n) as question_n
    from
        history_migrate.source_messages as s
),
question as (
    select
        u.thread_id,
        u.n,
        u.uuid as new_key,
        nullif(s.message ->> 'id', '') as old_key
    from
        history_migrate.units as u
        join history_migrate.source_messages as s
            on s.thread_id = u.thread_id
            and s.n = u.n
    where
        s.class = 'HumanMessage'
        and u.sub = 0
),
reply as (
    select
        t.thread_id,
        t.n,
        t.question_n,
        history_migrate.message_text(t.message -> 'content') <> '' as has_text,
        history_migrate.truthy(t.message -> 'additional_kwargs' -> 'error') as is_error,
        exists (
            select
                1
            from
                history_migrate.units as u
            where
                u.thread_id = t.thread_id
                and u.n = t.n
                and u.type = 'assistant'
                and u.body -> 'message' -> 'content' -> 0 ->> 'type' = 'text'
                and (
                    u.body -> 'message' ->> 'model' <> '<synthetic>'
                    or (u.body ->> 'isAbortedMidStream')::boolean
                )
        ) as replayed
    from
        turn as t
    where
        t.class = 'AIMessage'
        and t.question_n is not null
),
numbered as (
    select
        r.*,
        count(*) filter (where r.has_text or r.is_error) over w - 1 as old_number,
        count(*) filter (where r.replayed) over w - 1 as new_number
    from
        reply as r
    window w as (partition by r.thread_id, r.question_n order by r.n)
)
select
    m.thread_id,
    m.n,
    case
        when m.has_text and not m.is_error then history_migrate.answer_id(m.thread_id, q.old_key, m.old_number)
    end,
    case
        when m.replayed then history_migrate.answer_id(m.thread_id, q.new_key, m.new_number)
    end
from
    numbered as m
    join question as q
        on q.thread_id = m.thread_id
        and q.n = m.question_n;

create table history_migrate.feedback_moves (
    feedback_id uuid primary key,
    old_for_id  uuid not null,
    new_for_id  uuid not null
);

insert into history_migrate.feedback_moves (feedback_id, old_for_id, new_for_id)
select
    f.id,
    f.for_id,
    a.new_id
from
    :"chainlit_schema".feedbacks as f
    join history_migrate.answers as a
        on a.thread_id = f.thread_id::text
        and a.old_id = f.for_id
where
    a.new_id is not null
    and a.new_id <> f.for_id;

update
    :"chainlit_schema".feedbacks as f
set
    for_id = m.new_for_id
from
    history_migrate.feedback_moves as m
where
    f.id = m.feedback_id;

-- 8. Сверка.
--
-- Каждая проверка ниже ищет нарушение, которого при верном переносе быть
-- не может: вопрос или итог инструмента без своей записи, сессия без строки
-- в session, запись в истории, отличная от собранной, seq не подряд,
-- parentUuid не на предыдущую запись, ключ каркаса (type, seq, sessionId,
-- timestamp), оставшийся в документе вместо колонки. Если найдено хоть одно,
-- перенос откатывается целиком. Заметки перевода и треды без строки в threads
-- только выводятся.

create table history_migrate.problems (
    session_id text not null,
    seq        integer,
    problem    text not null
);

insert into history_migrate.problems (session_id, seq, problem)
select
    s.thread_id,
    null::integer,
    format('%s questions became %s question records.', s.questions, coalesce(r.questions, 0))
from (
    select thread_id, count(*) as questions
    from history_migrate.source_messages
    where class = 'HumanMessage'
    group by thread_id
) as s
left join (
    select session_id, count(*) as questions
    from history_migrate.records
    where record -> 'origin' ->> 'kind' = 'human'
    group by session_id
) as r
    on r.session_id = s.thread_id
where
    s.questions <> coalesce(r.questions, 0)
union all
select
    s.thread_id,
    null,
    format('%s tool results became %s tool result records.', s.results, coalesce(r.results, 0))
from (
    select thread_id, count(*) as results
    from history_migrate.source_messages
    where class = 'ToolMessage'
    group by thread_id
) as s
left join (
    select session_id, count(*) as results
    from history_migrate.records
    where record -> 'message' -> 'content' -> 0 ->> 'type' = 'tool_result'
    group by session_id
) as r
    on r.session_id = s.thread_id
where
    s.results <> coalesce(r.results, 0)
union all
select
    l.session_id,
    null,
    'The session has no row in the session table.'
from
    history_migrate.loaded as l
where
    not exists (select 1 from :"boba_schema".session as s where s.id = l.session_id)
union all
select
    l.session_id,
    null,
    format('The history holds %s records of the session, %s of them different from the %s built ones.', count(h.seq), count(h.seq) filter (where (h.type, h.recorded_at, h.record) is distinct from (r.type, r.recorded_at, r.record)), l.records)
from
    history_migrate.loaded as l
    left join :"boba_schema".session_records as h
        on h.session_id = l.session_id
    left join history_migrate.records as r
        on r.session_id = h.session_id
        and r.seq = h.seq
group by
    l.session_id,
    l.records
having
    count(h.seq) <> l.records
    or count(h.seq) filter (where (h.type, h.recorded_at, h.record) is distinct from (r.type, r.recorded_at, r.record)) > 0
union all
select
    session_id,
    seq,
    problem
from (
    select
        h.session_id,
        h.seq,
        case
            when h.seq <> row_number() over w then 'The seq of the record does not follow the previous one.'
            when h.record ?| array['type', 'seq', 'sessionId', 'timestamp'] then 'The record document keeps a key that belongs in the columns (type, seq, sessionId or timestamp).'
            when h.record ->> 'parentUuid' is distinct from lag(h.record ->> 'uuid') over w then 'The parentUuid of the record does not name the previous record.'
        end as problem
    from
        :"boba_schema".session_records as h
        join history_migrate.loaded as l
            on l.session_id = h.session_id
    window w as (partition by h.session_id order by h.seq)
) as checked
where
    problem is not null;

\echo 'Notes of the translation (they do not stop the migration):'
select thread_id, n, note from history_migrate.notes order by thread_id, n;

\echo 'Feedbacks moved to the answer steps the new chat draws:'
select feedback_id, old_for_id, new_for_id from history_migrate.feedback_moves order by feedback_id;

\echo 'Feedbacks left on steps the new chat does not draw (they stay in the table, the feed does not show them):'
select
    f.id as feedback_id,
    f.thread_id,
    f.for_id
from
    :"chainlit_schema".feedbacks as f
where
    not exists (
        select
            1
        from
            history_migrate.answers as a
        where
            a.thread_id = f.thread_id::text
            and a.new_id = f.for_id
    )
order by
    f.id;

\echo 'Migrated threads without a row in threads (the chat does not list them):'
select
    count(*) as threads_without_chat_row
from
    (select distinct thread_id from history_migrate.source_messages) as s
where
    not exists (select 1 from :"chainlit_schema".threads as t where t.id::text = s.thread_id);

\echo 'Result of the migration:'
select
    (select count(distinct thread_id) from history_migrate.source_messages) as threads_translated,
    (select count(*) from history_migrate.loaded) as sessions_loaded,
    (select coalesce(sum(records), 0) from history_migrate.loaded) as records_loaded,
    (select count(distinct thread_id) from history_migrate.source_messages)
        - (select count(*) from history_migrate.loaded) as sessions_already_in_history,
    (select count(*) from kept_by_chat) as threads_kept_by_chat,
    (select count(*) from history_migrate.bound) as threads_bound,
    (select count(*) from history_migrate.feedback_moves) as feedbacks_moved;

select count(*) > 0 as failed from history_migrate.problems \gset
\if :failed
    \echo 'The check found problems:'
    select session_id, seq, problem from history_migrate.problems order by session_id, seq;
    do $$ begin raise exception 'The check of the migration found problems (listed above), so nothing was changed.'; end $$;
\endif

-- 9. Приведение таблиц.
--
-- Таблицы чата users, threads и feedbacks новое приложение создаёт тем же
-- определением, что и dev, и при старте оставляет существующие как есть.
-- Таблица users, заведённая выпуском раньше dev, могла остаться без
-- умолчания у created_at: умолчание ставится, как в определении приложения.
-- Ключ llm в users.meta хранил выбор семплинга пользователя по профилям
-- (temperature, reasoning_effort); новое приложение его не читает: модель
-- и её настройки задаёт профиль чата, поэтому ключ удаляется. Таблицы шины
-- (live_*) и настроек агента треда (agent_chat_settings) приложение создаёт
-- само при старте.
--
-- Удаляются таблицы, которых в новом приложении нет: elements (вложения
-- вопросов теперь ссылки resource_link в истории агента, итог инструмента —
-- результат tools/call в toolUseResult) и чекпойнты langgraph вместе с их
-- схемой.

alter table :"chainlit_schema".users alter column created_at set default now();

update
    :"chainlit_schema".users
set
    meta = meta - 'llm'
where
    meta ? 'llm';

drop table :"chainlit_schema".elements;

drop table
    :"checkpoints_schema".checkpoint_writes,
    :"checkpoints_schema".checkpoint_blobs,
    :"checkpoints_schema".checkpoints;

select to_regclass(format('%I.checkpoint_migrations', :'checkpoints_schema')) is not null as has_checkpoint_migrations \gset
\if :has_checkpoint_migrations
drop table :"checkpoints_schema".checkpoint_migrations;
\endif

drop schema :"checkpoints_schema";

-- 10. Раскладка по схемам компонентов.
--
-- Новое приложение держит таблицы в трёх схемах: чат — в aid, агент — в
-- boba, сервис boba-mcp и его плагины — в mcp. Схема чата dev целиком
-- переименовывается в aid, а схема базы знаний — в mcp: переименование
-- переносит таблицы вместе с индексами, ограничениями, последовательностями
-- и функциями, и ссылки на них по oid (вычисляемая колонка kb_chunks.tsv
-- зовёт immutable_unaccent) остаются верными. Таблицы инструмента описаний
-- переносятся в mcp поштучно вместе со своими последовательностями, затем
-- пустая схема describer удаляется; если схемы базы знаний нет, describer
-- сама переименовывается в mcp. Схемы ix и studio остаются как есть.

select
    format('alter schema %I rename to %I', :'chainlit_schema', :'aid_schema')
where
    :'chainlit_schema' <> :'aid_schema'
\gexec

select
    format('alter schema %I rename to %I', :'kb_schema', :'mcp_schema')
where
    :'kb_schema' <> :'mcp_schema'
    and to_regnamespace(:'kb_schema') is not null
\gexec

select
    format('alter schema %I rename to %I', :'describer_schema', :'mcp_schema')
where
    :'describer_schema' <> :'mcp_schema'
    and to_regnamespace(:'describer_schema') is not null
    and to_regnamespace(:'mcp_schema') is null
\gexec

select
    format('alter table %I.%I set schema %I', n.nspname, c.relname, :'mcp_schema')
from
    pg_class as c
    join pg_namespace as n
        on n.oid = c.relnamespace
where
    n.nspname = :'describer_schema'
    and n.nspname <> :'mcp_schema'
    and c.relkind in ('r', 'p')
order by
    c.relname
\gexec

select
    format('drop schema %I', :'describer_schema')
where
    :'describer_schema' <> :'mcp_schema'
    and to_regnamespace(:'describer_schema') is not null
\gexec

-- 11. Удаление рабочей схемы и фиксация.

set local client_min_messages = warning;

drop schema history_migrate cascade;

commit;

\echo 'The migration is committed.'
