-- Переход идентификаторов на uuid: users (id := прежний user_uuid), threads.user_id,
-- connections/roles/grants, workflows/workflow_runs/workflow_drafts.
-- Запуск на каждую схему приложения:
--   psql -v schema=chainlit   -f 2026-08-29-uuid-ids.sql
--   psql -v schema=automation -f 2026-08-29-uuid-ids.sql
-- Таблицы live_* не переносятся: ленты эфемерны, схема live пересоздаётся приложением
-- (truncate live.live_locks — в ней user_id).
set search_path to :schema;

begin;

-- users: новый id — прежний user_uuid
alter table users add column id_new uuid;
update users set id_new = user_uuid;
alter table users alter column id_new set not null;

-- владельцы тредов (таблица есть только у чата)
do $$
begin
    if to_regclass('threads') is null then
        return;
    end if;

    alter table threads add column user_id_new uuid;
    update threads t set user_id_new = u.user_uuid from users u where u.id = t.user_id;
    alter table threads drop column user_id;
    alter table threads rename column user_id_new to user_id;
    create index if not exists idx_threads_user_id on threads (user_id);
end $$;

-- connections и roles: новые случайные id, старые остаются в *_old до пересборки grants
alter table connections add column id_new uuid not null default gen_random_uuid();
alter table roles add column id_new uuid not null default gen_random_uuid();

alter table grants add column src_kind_id_new uuid;
alter table grants add column tgt_kind_id_new uuid;
update grants g set src_kind_id_new = c.id_new from connections c
    where g.src_kind = 'connections' and c.id = g.src_kind_id;
update grants g set tgt_kind_id_new = u.user_uuid from users u
    where g.tgt_kind = 'users' and u.id = g.tgt_kind_id;
update grants g set tgt_kind_id_new = r.id_new from roles r
    where g.tgt_kind = 'roles' and r.id = g.tgt_kind_id;
delete from grants where src_kind_id_new is null or tgt_kind_id_new is null;
alter table grants drop column src_kind_id, drop column tgt_kind_id, drop column id;
alter table grants rename column src_kind_id_new to src_kind_id;
alter table grants rename column tgt_kind_id_new to tgt_kind_id;
alter table grants add column id uuid primary key default gen_random_uuid();
alter table grants alter column src_kind_id set not null, alter column tgt_kind_id set not null;
alter table grants add unique (src_kind, src_kind_id, tgt_kind, tgt_kind_id);
create index if not exists idx_grants_target on grants (tgt_kind, tgt_kind_id);

alter table connections drop column id;
alter table connections rename column id_new to id;
alter table connections add primary key (id);
alter table roles drop column id;
alter table roles rename column id_new to id;
alter table roles add primary key (id);

-- workflows: новый id, владелец по users
alter table workflows add column id_new uuid not null default gen_random_uuid();
alter table workflows add column user_id_new uuid;
update workflows w set user_id_new = u.user_uuid from users u where u.id = w.user_id;
delete from workflows where user_id_new is null;

alter table workflow_runs add column workflow_id_new uuid;
alter table workflow_runs add column user_id_new uuid;
update workflow_runs r set workflow_id_new = w.id_new from workflows w where w.id = r.workflow_id;
update workflow_runs r set user_id_new = u.user_uuid from users u where u.id = r.user_id;
delete from workflow_runs where user_id_new is null;
alter table workflow_runs drop column workflow_id, drop column user_id;
alter table workflow_runs rename column workflow_id_new to workflow_id;
alter table workflow_runs rename column user_id_new to user_id;
alter table workflow_runs alter column user_id set not null;

alter table workflow_drafts add column user_id_new uuid;
update workflow_drafts d set user_id_new = u.user_uuid from users u where u.id = d.user_id;
delete from workflow_drafts where user_id_new is null;
alter table workflow_drafts drop column user_id;
alter table workflow_drafts rename column user_id_new to user_id;
alter table workflow_drafts alter column user_id set not null;
alter table workflow_drafts add primary key (user_id, key);

alter table workflows drop column id, drop column user_id;
alter table workflows rename column id_new to id;
alter table workflows rename column user_id_new to user_id;
alter table workflows alter column user_id set not null;
alter table workflows add primary key (id);
alter table workflows add unique (user_id, name);
alter table workflow_runs add foreign key (workflow_id) references workflows (id) on delete set null;
create index if not exists idx_workflow_runs_user on workflow_runs (user_id, started_at desc);

-- users: старый id и user_uuid уходят
alter table users drop column id, drop column user_uuid;
alter table users rename column id_new to id;
alter table users add primary key (id);
alter table users alter column id set default gen_random_uuid();

commit;
