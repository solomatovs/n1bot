# Шаг prepare: каталог share/ чата. UI chainlit собирается из исходников тега с overlay
# web/chainlit-ui по всем пакетам workspace — react-client, app (фронт) и copilot
# (виджет), как pnpm build у upstream: custom_build chainlit резолвит из одного
# каталога и index.html фронта, и index.js copilot. Зависимости pnpm и npm — из
# реестра npm образа nodejs (в закрытом контуре — nexus). Итог — стадия share
ARG NODE_IMAGE=dmp/nodejs:20
ARG OS_IMAGE

FROM ${OS_IMAGE} AS conf


# store pnpm: слой зависит только от тарболла тега и манифестов overlay, поэтому
# правка исходников overlay его не перекачивает
FROM ${NODE_IMAGE} AS store

ARG CHAINLIT_VERSION

ENV HOME=/tmp HUSKY=0 CYPRESS_INSTALL_BINARY=0 \
    npm_config_update_notifier=false npm_config_manage_package_manager_versions=false \
    PNPM_OPTS="--frozen-lockfile --network-concurrency=8 --config.package-manager-strict=false --filter @chainlit/react-client --filter @chainlit/app --filter @chainlit/copilot"

COPY --from=conf /etc/ssl/certs/                     /etc/ssl/certs/
COPY --from=conf /usr/local/share/ca-certificates/   /usr/local/share/ca-certificates/
COPY build/src/chainlit-${CHAINLIT_VERSION}.tar.gz   /tmp/chainlit.tar.gz
COPY packages/apps/boba-aid/web/chainlit-ui/overlay/frontend/package.json \
     packages/apps/boba-aid/web/chainlit-ui/overlay/frontend/pnpm-lock.yaml  /tmp/manifests/frontend/

WORKDIR /tmp/ui
RUN root=$(tar -tzf /tmp/chainlit.tar.gz | head -1 | cut -d/ -f1) \
    && tar -xzf /tmp/chainlit.tar.gz --strip-components=1 \
        "$root/frontend" "$root/libs" "$root/package.json" "$root/pnpm-lock.yaml" \
        "$root/pnpm-workspace.yaml" "$root/.npmrc"
RUN cp -a /tmp/manifests/. /tmp/ui/ && pnpm install --store-dir /pnpm-store $PNPM_OPTS \
    && rm -rf /tmp/ui


# UI chainlit: исходники тега, overlay поверх, элементы chainlit копией в
# frontend/elements (их классы должны попасть в CSS сборки), сборка трёх пакетов,
# CDN в index.html заменяется на public/vendor, проверка контракта custom_build и
# .source-digest — версия тега и хеши overlay, элементов и этого файла: по нему
# тест вида узнаёт, что сборка устарела
FROM store AS ui

COPY packages/apps/boba-aid/web/chainlit-ui/overlay/  /tmp/overlay/
COPY packages/apps/boba-aid/assets/public/elements/   /tmp/elements/
COPY build/ui.Dockerfile                              /tmp/ui.Dockerfile

WORKDIR /tmp/ui
RUN root=$(tar -tzf /tmp/chainlit.tar.gz | head -1 | cut -d/ -f1) \
    && tar -xzf /tmp/chainlit.tar.gz --strip-components=1 \
        "$root/frontend" "$root/libs" "$root/package.json" "$root/pnpm-lock.yaml" \
        "$root/pnpm-workspace.yaml" "$root/.npmrc" \
    && cp -a /tmp/overlay/. /tmp/ui/ \
    && pnpm install --store-dir /pnpm-store $PNPM_OPTS \
    && mkdir -p frontend/elements && cp -a /tmp/elements/. frontend/elements/
RUN pnpm --filter @chainlit/react-client run build \
    && pnpm --filter @chainlit/react-client run type-check \
    && pnpm --filter @chainlit/app run type-check \
    && pnpm --filter @chainlit/app run build \
    && pnpm --filter @chainlit/copilot run build
RUN sed -i \
        -e '/rel="preconnect"/d' \
        -e 's#https://fonts.googleapis.com/css2?family=Inter[^"]*#/public/vendor/inter/inter.css#' \
        -e 's#https://cdn.jsdelivr.net/npm/katex@[0-9.]*/dist/katex.min.css#/public/vendor/katex/katex.min.css#' \
        frontend/dist/index.html \
    && ! grep -E 'fonts\.(googleapis|gstatic)\.com|cdn\.jsdelivr\.net' frontend/dist/index.html
RUN mkdir -p /out/ui && cp -a frontend/dist/. /out/ui/ && cp -a libs/copilot/dist/. /out/ui/ \
    && test -f /out/ui/index.html && test -f /out/ui/index.js
RUN digest() { (cd "$1" && find . -type f | LC_ALL=C sort | xargs sha256sum) | sha256sum | cut -d' ' -f1; } \
    && root=$(tar -tzf /tmp/chainlit.tar.gz | head -1 | cut -d/ -f1) \
    && { echo "chainlit ${root#chainlit-}"; \
         echo "overlay $(digest /tmp/overlay)"; \
         echo "elements $(digest /tmp/elements)"; \
         echo "script $(sha256sum /tmp/ui.Dockerfile | cut -d' ' -f1)"; } > /out/ui/.source-digest


# скрипт страницы chainlit (web/page, TypeScript)
FROM ${NODE_IMAGE} AS page

ENV HOME=/tmp npm_config_update_notifier=false

COPY --from=conf /etc/ssl/certs/                    /etc/ssl/certs/
COPY --from=conf /usr/local/share/ca-certificates/  /usr/local/share/ca-certificates/
COPY packages/apps/boba-aid/web/page/  /src/web/page/

WORKDIR /src/web/page
RUN npm ci --no-audit --no-fund && npm run check && npm run build && test -f /src/assets/public/page.js


# share/ чата: общий конфиг chainlit, ассеты, UI, page.js, vendor-шрифты и стили
# формул вместо CDN, страница песочницы MCP Apps
FROM scratch AS share

COPY packages/apps/boba-aid/assets/.chainlit/config.toml    /share/.chainlit/
COPY packages/apps/boba-aid/assets/.chainlit/translations/  /share/.chainlit/translations/
COPY packages/apps/boba-aid/assets/chainlit.md              /share/
COPY packages/apps/boba-aid/assets/public/                  /share/public/
COPY packages/apps/boba-aid/assets/mcp-apps/                /share/mcp-apps/
COPY --from=page /src/assets/public/page.js                 /share/public/page.js
COPY --from=ui /out/ui/                                     /share/ui/
COPY build/src/webassets/                                   /share/public/vendor/
