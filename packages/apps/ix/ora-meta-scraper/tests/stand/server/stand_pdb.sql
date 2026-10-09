-- PDB скрапера из PDB$SEED: словарь у каждой PDB свой, поэтому DDL насосов и инструментов
-- в основной PDB не сбивает обход скрапера. &1 — каталог oradata экземпляра, &2 — имя PDB:
-- SCRAPEPDB для эталона и шторма, BULKPDB для нагрузочного словаря теста памяти;
-- &3 — пароль пользователя скрапера boba_svc.
set pages 0 feedback off verify off
whenever sqlerror exit failure
create pluggable database &2 admin user pdbadmin identified by oracle
    file_name_convert = ('&1/pdbseed/', '&1/&2/');
alter pluggable database &2 open;
alter pluggable database &2 save state;
alter session set container = &2;
create tablespace users datafile '&1/&2/users01.dbf' size 100m autoextend on next 50m;
create user boba_svc identified by "&3" default tablespace users quota unlimited on users;
grant create session to boba_svc;
exit
