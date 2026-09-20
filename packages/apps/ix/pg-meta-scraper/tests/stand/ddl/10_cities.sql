-- @min 80300
create table dm.cities (name text primary key, population int);
create table dm.capitals (country text) inherits (dm.cities);
