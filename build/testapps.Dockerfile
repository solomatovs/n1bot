# Образ boba-testapps — соседи тестов клиента MCP, не поставка: официальные примеры MCP Apps
# (server-customer-segmentation на 3101, server-debug на 3105) и свой сервер на TypeScript SDK
# 1.x (legacy-server.mjs) в четырёх редакциях на 3110-3113 (serve.sh). Пакеты ставит npm ci по
# package-lock.json из реестра npm образа nodejs. Контекст — каталог tests/services/testapps.
# Собирает make testapps
ARG NODE_IMAGE=dmp/nodejs:22
FROM ${NODE_IMAGE}

# область кэша make offline-check; npm не спрашивает реестр о своей новой версии
ARG CACHE_SCOPE
ENV npm_config_update_notifier=false

WORKDIR /srv
COPY package.json package-lock.json /srv/
RUN npm ci --no-audit --no-fund --omit=dev && npm cache clean --force
COPY legacy-server.mjs serve.sh /srv/

EXPOSE 3101 3105 3110 3111 3112 3113
ENTRYPOINT ["sh", "/srv/serve.sh"]
