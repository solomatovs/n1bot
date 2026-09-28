#!/bin/sh
# Стенд ClickHouse: <prefix>-<ver> из локальных образов dmp/clickhouse с конфигом этого каталога.
# edge-ch — серверы насосов и инструментов, edge-chs — только скрапера: он обходит сервер
# целиком и не терпит чужого DDL во время обхода.
set -e
if [ -z "$1" ]; then
    echo "usage: $0 <prefix>   (edge-ch | edge-chs)" >&2
    exit 2
fi
prefix=$1
here=$(cd "$(dirname "$0")" && pwd)
for spec in "22.12 22.12.6.22-stable" "23.12 23.12.6.19-stable" "24.12 24.12.6.70-stable" "25.12 25.12.12.1-stable" "26.7 26.7.1.1-new"; do
    set -- $spec
    docker rm -f "$prefix-$1" >/dev/null 2>&1 || true
    keeper=""
    if [ "$1" = "26.7" ]; then
        keeper="-v $here/keeper.xml:/etc/clickhouse-server/config.d/keeper.xml:ro"
    fi
    docker run -d --name "$prefix-$1" --restart unless-stopped \
        -v "$here/config.xml:/etc/clickhouse-server/config.xml:ro" \
        -v "$here/users.xml:/etc/clickhouse-server/users.xml:ro" \
        $keeper \
        --entrypoint sh "dmp/clickhouse:$2" -c \
        'mkdir -p /var/lib/clickhouse && exec clickhouse server --config-file=/etc/clickhouse-server/config.xml' >/dev/null
done
sleep 6
for v in 22.12 23.12 24.12 25.12 26.7; do
    ip=$(docker inspect "$prefix-$v" -f '{{.NetworkSettings.Networks.bridge.IPAddress}}')
    echo "$prefix-$v $ip $(curl -s -u scraper:scraper "http://$ip:8123/?query=select%20version()%2C%20currentUser()")"
done
