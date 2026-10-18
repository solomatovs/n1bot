# Образ boba-testapps — соседи тестов клиента MCP, не поставка: официальные примеры MCP Apps
# (server-customer-segmentation на 3101, server-debug на 3105) и свой сервер редакции 2025-11-25
# на TypeScript SDK 1.x (3110, legacy-server.mjs). Пакеты ставит npm ci по package-lock.json из
# реестра npm образа nodejs. Контекст — каталог tests/services/testapps. Собирает make testapps
ARG NODE_IMAGE=dmp/nodejs:22
FROM ${NODE_IMAGE}

WORKDIR /srv
COPY package.json package-lock.json /srv/
RUN npm ci --no-audit --no-fund --omit=dev && npm cache clean --force
COPY legacy-server.mjs /srv/

EXPOSE 3101 3105 3110
ENTRYPOINT ["sh", "-c", "PORT=3101 node node_modules/@modelcontextprotocol/server-customer-segmentation/dist/index.js & PORT=3105 node node_modules/@modelcontextprotocol/server-debug/dist/index.js & node legacy-server.mjs 3110 & wait"]
