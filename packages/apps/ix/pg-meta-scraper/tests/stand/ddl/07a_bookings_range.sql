-- @min 90200
-- @not gp
create extension if not exists btree_gist;
create table dm.bookings (
    id      bigserial primary key,
    room    int       not null,
    during  tstzrange not null,
    exclude using gist (room with =, during with &&)
);
