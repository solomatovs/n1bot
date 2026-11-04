-- Запускать отдельной транзакцией при остановленном приложении.
-- {schema} подставляется как SQL identifier, не как строковое значение.
alter table {schema}.users add column realm text, add column login text;

update {schema}.users
set realm = meta -> 'identity' ->> 'realm',
    login = meta -> 'identity' ->> 'login'
where meta ? 'identity';

do $$
begin
    if exists (
        select 1 from {schema}.users
        where meta ? 'identity' and (realm is null or login is null)
    ) then
        raise exception 'users contain malformed identity metadata';
    end if;
end;
$$;

alter table {schema}.users add constraint users_identity check (
    (realm is null and login is null)
    or (realm is not null and login is not null
        and realm ~ '^[a-z0-9][a-z0-9._-]*$'
        and length(login) > 0 and login = lower(btrim(login))
        and identifier = realm || ':' || login)
);

alter table {schema}.user_roles add column managed boolean not null default false;
-- До этого изменения все сохранённые членства синхронизировал вход.
update {schema}.user_roles set managed = true;

alter table {schema}.users drop column meta, drop column if exists tags;
