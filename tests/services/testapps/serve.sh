#!/bin/sh
# Вход образа boba-testapps: примеры MCP Apps, свой сервер в четырёх редакциях и
# серверы fastmcp[apps] с рендерером Prefab статикой на адресе контейнера
cd /srv
PKG=node_modules/@modelcontextprotocol
PORT=3101 node $PKG/server-customer-segmentation/dist/index.js &
PORT=3105 node $PKG/server-debug/dist/index.js &
node legacy-server.mjs 3110 &
node legacy-server.mjs 3111 --handshake 2025-06-18 &
node legacy-server.mjs 3112 --modern 2027-01-01 --handshake 2025-06-18,2025-03-26 &
node legacy-server.mjs 3113 --modern 2027-01-01 --handshake "" &

IP=$(hostname -i | cut -d' ' -f1)
py() {
    LD_LIBRARY_PATH=/python/lib PYTHONHOME=/python PREFAB_RENDERER_URL="http://$IP:3124" \
        /python/bin/python3 /srv/apps_server.py "$@"
}
py prefab 3120 &
py notes 3121 &
py genui 3122 &
py renderer 3124 &
wait
