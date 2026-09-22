#!/bin/sh
# Стенд ora-meta-scraper: edge-ora-<ver> из образов gvenzl (XE 18, 21 и Free 23) и
# гранты пользователю scraper на системные таблицы словаря. Контейнер oracle 12.2
# (compose/oracle) поднимается отдельно, гранты ему выдаёт grants.sql тем же способом.
set -e
here=$(cd "$(dirname "$0")" && pwd)
for spec in "18 gvenzl/oracle-xe:18-slim XEPDB1" "21 gvenzl/oracle-xe:21-slim XEPDB1" "23 gvenzl/oracle-free:23-slim FREEPDB1"; do
    set -- $spec
    docker rm -f "edge-ora-$1" >/dev/null 2>&1 || true
    docker run -d --name "edge-ora-$1" --restart unless-stopped --shm-size=1g \
        -e ORACLE_PASSWORD=oracle -e APP_USER=scraper -e APP_USER_PASSWORD=scraper \
        "$2" >/dev/null
done
for spec in "18 XEPDB1" "21 XEPDB1" "23 FREEPDB1"; do
    set -- $spec
    until docker logs "edge-ora-$1" 2>&1 | grep -q 'DATABASE IS READY TO USE'; do sleep 5; done
    docker cp "$here/grants.sql" "edge-ora-$1:/tmp/grants.sql"
    docker exec "edge-ora-$1" bash -lc "printf 'alter session set container=$2;\n@/tmp/grants.sql\n' | sqlplus -s / as sysdba"
    ip=$(docker inspect "edge-ora-$1" -f '{{.NetworkSettings.Networks.bridge.IPAddress}}')
    echo "edge-ora-$1 $ip service $2"
done
