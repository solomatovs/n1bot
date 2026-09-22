create schema ref;
create schema dm;
create domain dm.email_t as text check (value ~ '^[^@]+@[^@]+$');
create type dm.order_status as enum ('open', 'paid', 'cancelled');
create table ref.currencies (
    code  char(3) primary key,
    name  text    not null
);
