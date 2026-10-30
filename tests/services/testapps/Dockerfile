# Образ boba-testapps — соседи тестов клиента MCP, не поставка: официальные примеры MCP Apps
# (server-customer-segmentation на 3101, server-debug на 3105), свой сервер на TypeScript SDK
# 1.x (legacy-server.mjs) в четырёх редакциях на 3110-3113 и серверы fastmcp[apps]
# (apps_server.py) на 3120-3122 с рендерером Prefab статикой на 3124 (serve.sh). Пакеты npm
# ставит npm ci по package-lock.json из реестра npm образа nodejs, dist рендерера — npm pack
# по пину; python — дерево образа python, пакеты по requirements.txt из реестра pypi.
# CA и pip.conf закрытого контура — те же, что у образа сборки BASE_IMAGE (build/conf).
# Контекст — каталог tests/services/testapps. Собирает make build APP=testapps
ARG NODE_IMAGE=dmp/nodejs:22
ARG PYTHON_IMAGE=dmp/python:3.11-dist
ARG BASE_IMAGE

FROM ${PYTHON_IMAGE} AS python-dist

FROM ${BASE_IMAGE} AS site-conf

FROM ${NODE_IMAGE}

# npm не спрашивает реестр о своей новой версии
ARG PREFAB_UI_VERSION=0.20.2
ENV npm_config_update_notifier=false PIP_CERT=/etc/ssl/certs/ca-certificates.crt

COPY --from=site-conf /etc/ssl/certs/                  /etc/ssl/certs/
COPY --from=site-conf /usr/local/share/ca-certificates/ /usr/local/share/ca-certificates/
COPY --from=site-conf /etc/pip.conf                    /etc/pip.conf

WORKDIR /srv
COPY package.json package-lock.json /srv/
RUN npm ci --no-audit --no-fund --omit=dev && npm cache clean --force

RUN npm pack "@prefecthq/prefab-ui@${PREFAB_UI_VERSION}" --silent --pack-destination /tmp \
    && mkdir -p /srv/prefab-ui \
    && tar xzf "/tmp/prefecthq-prefab-ui-${PREFAB_UI_VERSION}.tgz" -C /srv/prefab-ui \
           --strip-components=3 package/dist/app \
    && rm "/tmp/prefecthq-prefab-ui-${PREFAB_UI_VERSION}.tgz" && npm cache clean --force \
    && test -f /srv/prefab-ui/renderer.js && test -f /srv/prefab-ui/renderer.css

COPY --from=python-dist /opt/python/usr/local /python
COPY requirements.txt /srv/
RUN LD_LIBRARY_PATH=/python/lib PYTHONHOME=/python \
        /python/bin/python3 -m pip install --no-cache-dir --disable-pip-version-check \
        -r /srv/requirements.txt \
    && LD_LIBRARY_PATH=/python/lib PYTHONHOME=/python /python/bin/python3 -c \
        "import fastmcp.apps.app, fastmcp.apps.generative, prefab_ui.renderer; print('fastmcp apps ok')"

COPY legacy-server.mjs apps_server.py serve.sh /srv/

EXPOSE 3101 3105 3110 3111 3112 3113 3120 3121 3122 3124
ENTRYPOINT ["sh", "/srv/serve.sh"]
