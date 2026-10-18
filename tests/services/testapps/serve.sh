#!/bin/sh
# Вход образа boba-testapps: примеры MCP Apps и свой сервер в четырёх редакциях
cd /srv
PKG=node_modules/@modelcontextprotocol
PORT=3101 node $PKG/server-customer-segmentation/dist/index.js &
PORT=3105 node $PKG/server-debug/dist/index.js &
node legacy-server.mjs 3110 &
node legacy-server.mjs 3111 --handshake 2025-06-18 &
node legacy-server.mjs 3112 --modern 2027-01-01 --handshake 2025-06-18,2025-03-26 &
node legacy-server.mjs 3113 --modern 2027-01-01 --handshake "" &
wait
