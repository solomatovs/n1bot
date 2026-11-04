create table if not exists {schema}.role_tools (
    role_id uuid not null references {schema}.roles(id) on delete cascade,
    tool text not null check (length(btrim(tool)) > 0),
    primary key (role_id, tool)
);
