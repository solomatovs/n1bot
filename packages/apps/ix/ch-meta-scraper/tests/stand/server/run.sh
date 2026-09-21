#!/bin/sh
# Стенд ch-meta-scraper: edge-ch-<ver> из локальных образов dmp/clickhouse с конфигом этого каталога.
set -e
here=$(cd "$(dirname "$0")" && pwd)
for spec in "22.12 22.12.6.22-stable" "23.12 23.12.6.19-stable" "24.12 24.12.6.70-stable" "25.12 25.12.12.1-stable" "26.7 26.7.1.1-new"; do
    set -- $spec
    docker rm -f "edge-ch-$1" >/dev/null 2>&1 || true
    docker run -d --name "edge-ch-$1" --restart unless-stopped \
        -v "$here/config.xml:/etc/clickhouse-server/config.xml:ro" \
        -v "$here/users.xml:/etc/clickhouse-server/users.xml:ro" \
        --entrypoint sh "dmp/clickhouse:$2" -c \
        'mkdir -p /var/lib/clickhouse && exec clickhouse server --config-file=/etc/clickhouse-server/config.xml' >/dev/null
done
sleep 6
for v in 22.12 23.12 24.12 25.12 26.7; do
    ip=$(docker inspect "edge-ch-$v" -f '{{.NetworkSettings.Networks.bridge.IPAddress}}')
    echo "edge-ch-$v $ip $(curl -s -u scraper:scraper "http://$ip:8123/?query=select%20version()%2C%20currentUser()")"
done
