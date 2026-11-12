ARG PYTHON_VERSION=3.11
ARG GLIBC_IMAGE=dmp/glibc:2.28
ARG GCC_IMAGE=dmp/gcc:8.5.0
ARG PYTHON_IMAGE=dmp/python:${PYTHON_VERSION}-dist
ARG NODE_IMAGE=dmp/nodejs:20
ARG SANDBOX_IMAGE=scratch
ARG PLUGIN_IMAGE=plugin-output

FROM ${PYTHON_IMAGE} AS python-dist


# python рантайма без сборочного и учебного балласта: одно дерево для образа
# приложения и корней плагинов. Сборочные стадии берут полный python-dist
FROM ${GLIBC_IMAGE} AS python-slim

ARG PYTHON_VERSION=3.11

COPY --from=python-dist /opt/python/usr/local  /python/

RUN lib="/python/lib/python${PYTHON_VERSION}" \
    && rm -rf /python/include /python/share /python/lib/pkgconfig \
              /python/bin/2to3* /python/bin/idle3* /python/bin/pip* /python/bin/pydoc3* \
              /python/bin/python*-config \
              "$lib/test" "$lib/idlelib" "$lib/tkinter" "$lib/lib2to3" "$lib/ensurepip" \
              "$lib/config-${PYTHON_VERSION}-x86_64-linux-gnu" \
              "$lib/site-packages/pip" "$lib/site-packages/setuptools" \
              "$lib/site-packages/pkg_resources" "$lib/site-packages/"*.dist-info


# закрытый контур: CA внутреннего TLS и индексы пакетов из build/conf.
# Стадии берут готовые деревья: /certs — всем, /index — только сборочным
FROM ${GLIBC_IMAGE} AS site-conf

COPY build/conf/  /tmp/conf/

RUN mkdir -p /certs/etc/ssl /certs/usr/local/share /index/etc/uv

RUN if [ -f /tmp/conf/ca-chain.crt ]; then \
        cp /tmp/conf/ca-chain.crt /usr/local/share/ca-certificates/ && update-ca-certificates; \
    fi
RUN cp -a /etc/ssl/certs /certs/etc/ssl/ \
    && cp -a /usr/local/share/ca-certificates /certs/usr/local/share/

RUN if [ -f /tmp/conf/pip.conf ]; then cp /tmp/conf/pip.conf /index/etc/pip.conf; fi
RUN if [ -f /tmp/conf/uv.toml ]; then cp /tmp/conf/uv.toml /index/etc/uv/uv.toml; fi


# среда сборки: gcc + наш python поверх системного, uv, CA и индексы контура
FROM ${GCC_IMAGE} AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0                \
        liblzma5 libreadline7 libncurses5 zlib1g                 \
        libkrb5-dev                                              \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python-dist /opt/python  /
COPY --from=site-conf /certs/        /
COPY --from=site-conf /index/        /
COPY build/out/uv/uv        /usr/local/bin/uv

# число заданий компилирующих шагов: наследуют все сборочные стадии, uv читает сам
ARG JOBS=4
ENV JOBS=${JOBS} UV_CONCURRENT_BUILDS=${JOBS} UV_CONCURRENT_INSTALLS=${JOBS}

RUN ldconfig

# у каждой программы свой способ узнать CA bundle
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt        \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt   \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt             \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt       \
    GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt


# python-oracledb с патчем: sdist ровно той версии из индекса pypi с проверкой sha256,
# патч из scripts/, локальный суффикс версии — чтобы установщик не подменил колесо
# готовым с индекса. Колесо уходит в boba-wheels и в out/oracledb для .venv хоста
FROM base AS oracledb-wheel

ARG ORACLEDB_VERSION=26.0.0
ARG ORACLEDB_SHA256=d15728bae7546984203ae17f8296153d52d10ffdaf4929bebe46b70d5819d270
ARG ORACLEDB_LOCAL=boba.1
ARG CYTHON_VERSION=3.3.0
ARG SETUPTOOLS_VERSION=84.0.0
ARG WHEEL_VERSION=0.48.0

RUN apt-get update && apt-get install -y --no-install-recommends patch \
    && rm -rf /var/lib/apt/lists/*

COPY build/scripts/oracledb-${ORACLEDB_VERSION}-arrow-duplicates.patch  /tmp/oracledb.patch

RUN pip3 install --no-cache-dir "cython==${CYTHON_VERSION}" \
        "setuptools==${SETUPTOOLS_VERSION}" "wheel==${WHEEL_VERSION}"
RUN pip3 download --no-cache-dir --no-deps --no-binary :all: --no-build-isolation \
        "oracledb==${ORACLEDB_VERSION}" -d /tmp/sdist
RUN echo "${ORACLEDB_SHA256}  /tmp/sdist/oracledb-${ORACLEDB_VERSION}.tar.gz" | sha256sum -c -

WORKDIR /tmp/oracledb-src
RUN tar -xzf /tmp/sdist/oracledb-${ORACLEDB_VERSION}.tar.gz --strip-components=1
RUN patch -p1 < /tmp/oracledb.patch
RUN sed -i "s/^__version__ = \"${ORACLEDB_VERSION}\"$/__version__ = \"${ORACLEDB_VERSION}+${ORACLEDB_LOCAL}\"/" \
        src/oracledb/version.py \
    && grep -q "^__version__ = \"${ORACLEDB_VERSION}+${ORACLEDB_LOCAL}\"$" src/oracledb/version.py
# без отладочных символов: колесо с ними в четыре раза тяжелее
RUN CFLAGS="-g0" pip3 wheel . --no-cache-dir --no-deps --no-build-isolation -w /out && ls -l /out


# bubblewrap статически с libcap: у meson bwrap нет такой опции, поэтому
# в сгенерированном build.ninja .so подменяется на .a
FROM base AS bwrap-build

ARG BWRAP_VERSION=0.11.2

RUN apt-get update && apt-get install -y --no-install-recommends \
        libcap-dev meson ninja-build pkg-config xz-utils         \
    && rm -rf /var/lib/apt/lists/*

COPY build/src/bubblewrap-${BWRAP_VERSION}.tar.xz  /tmp/bwrap.tar.xz

WORKDIR /tmp/bwrap-src
RUN tar -xJf /tmp/bwrap.tar.xz --strip-components=1
RUN meson setup _build --buildtype=release --prefix=/usr -Dselinux=disabled -Dman=disabled \
    && sed -i 's|/usr/lib/x86_64-linux-gnu/libcap\.so|/usr/lib/x86_64-linux-gnu/libcap.a|g' _build/build.ninja \
    && ninja -j "${JOBS}" -C _build bwrap
RUN strip _build/bwrap && install -D -m 0755 _build/bwrap /out/bwrap


# fuse2fs статически с libfuse, плюс шаблон workspace тем же mke2fs, что уедет в релиз
FROM base AS fuse2fs-build

ARG LIBFUSE_VERSION=3.12.0
ARG E2FSPROGS_VERSION=1.47.2

RUN apt-get update && apt-get install -y --no-install-recommends \
        meson ninja-build pkg-config xz-utils                    \
    && rm -rf /var/lib/apt/lists/*

COPY build/src/fuse-${LIBFUSE_VERSION}.tar.xz         /tmp/fuse.tar.xz
COPY build/src/e2fsprogs-${E2FSPROGS_VERSION}.tar.xz  /tmp/e2fsprogs.tar.xz

WORKDIR /tmp/fuse-src
RUN tar -xJf /tmp/fuse.tar.xz --strip-components=1
RUN meson setup _build --buildtype=release --prefix=/usr/local \
        -Ddefault_library=static -Dexamples=false -Dtests=false -Dutils=false \
    && ninja -j "${JOBS}" -C _build install

WORKDIR /tmp/e2fs-src
ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig:/usr/local/lib/x86_64-linux-gnu/pkgconfig
RUN tar -xJf /tmp/e2fsprogs.tar.xz --strip-components=1
RUN LIBS="-lpthread -ldl" ./configure --enable-fuse2fs --disable-nls && make -j"${JOBS}"
RUN strip misc/fuse2fs && install -D -m 0755 misc/fuse2fs /out/fuse2fs

RUN ! ldd /out/fuse2fs | grep fuse && /out/fuse2fs -V

# mke2fs линкован только с libc и создаёт ext4 без монтирования.
RUN install -D -m 0755 misc/mke2fs /out/mke2fs && cp misc/mke2fs.conf /out/mke2fs.conf


FROM ${GLIBC_IMAGE} AS ext4-tools

COPY --from=fuse2fs-build /out/mke2fs       /tools/mke2fs
COPY --from=fuse2fs-build /out/mke2fs.conf  /tools/mke2fs.conf
ENV MKE2FS_CONFIG=/tools/mke2fs.conf


FROM ext4-tools AS workspace-image

ARG SANDBOX_WS_SIZE=1G

RUN mkdir -p /out \
    && truncate -s "${SANDBOX_WS_SIZE}" /out/workspace.ext4 \
    && /tools/mke2fs -F -q -t ext4 -O ^has_journal -m 0 -L workspace /out/workspace.ext4 \
    && chmod 0644 /out/workspace.ext4


# колёса всех пакетов репозитория и constraints из uv.lock: обе стадии установки
# получают версии хоста, иначе uv ставил бы свежайшие
FROM base AS boba-wheels

COPY pyproject.toml  /tmp/boba/pyproject.toml
COPY uv.lock         /tmp/boba/uv.lock
COPY packages/       /tmp/boba/packages/

COPY --from=oracledb-wheel /out/  /tmp/wheels/
# find-links корневого pyproject ведёт сюда: колесо с патчем лежит там же, где
# на хосте его держит build/out
COPY --from=oracledb-wheel /out/  /tmp/boba/build/out/oracledb/

WORKDIR /tmp/boba
RUN uv build --all-packages --wheel --no-sources -o /tmp/wheels
RUN uv export --frozen --all-packages --all-extras --no-emit-workspace --no-hashes \
        -o /tmp/wheels/constraints.txt


# --- образ корня одного плагина: make plugin-rootfs PLUGIN=<пакет> ---------------

# site-packages плагина: закрытие payload одного пакета и его сборочные декларации
FROM base AS plugin-site

ARG PLUGIN

COPY --from=boba-wheels /tmp/wheels/          /tmp/wheels/
COPY --from=boba-wheels /tmp/boba/packages/   /tmp/packages/
COPY build/scripts/plugin_rootfs.py  /tmp/plugin_rootfs.py

RUN mkdir -p /opt/site /opt/code-sandbox /opt/plugin-root /opt/plugin-setup.d \
    && : > /opt/plugin-apt.txt && : > /opt/plugin-data.txt

RUN [ -z "${PLUGIN}" ] || { \
        python3 /tmp/plugin_rootfs.py names /tmp/packages ${PLUGIN} > /tmp/names.txt && cat /tmp/names.txt; }
RUN [ -z "${PLUGIN}" ] || uv pip install --python "$(command -v python3)" --target /opt/site \
        --constraint /tmp/wheels/constraints.txt --find-links=/tmp/wheels \
        $(cat /tmp/names.txt)

RUN [ -z "${PLUGIN}" ] || python3 /tmp/plugin_rootfs.py apt /tmp/packages ${PLUGIN} > /opt/plugin-apt.txt
RUN [ -z "${PLUGIN}" ] || python3 /tmp/plugin_rootfs.py data /tmp/packages ${PLUGIN} > /opt/plugin-data.txt

RUN [ -z "${PLUGIN}" ] || for root in $(python3 /tmp/plugin_rootfs.py root /tmp/packages ${PLUGIN}); do \
        cp -a "$root/." /opt/plugin-root/; \
    done

RUN [ -z "${PLUGIN}" ] || { index=0 \
    && for setup in $(python3 /tmp/plugin_rootfs.py setup /tmp/packages ${PLUGIN}); do \
           cp "$setup" "/opt/plugin-setup.d/$index.sh"; index=$((index + 1)); \
       done; }

ENV PYTHONPATH=/opt/site

RUN [ -z "${PLUGIN}" ] || { \
        mv /opt/site/boba /opt/code-sandbox/boba \
        && mv /opt/site/boba_*.dist-info /opt/code-sandbox/ \
        && echo /usr/src > /opt/site/boba-src.pth; }


# общая база корней плагинов: glibc, нативные зависимости, одинаковые у всех
# плагинов, libstdc++ сборочного gcc и python. Одна на все плагины и версии
FROM ${GLIBC_IMAGE} AS sandbox-base

ARG LIBSTDCXX_VERSION=6.0.25
ARG LIBSTDCXX_OLD=6.0.22

COPY --from=site-conf /certs/  /

RUN apt-get update && apt-get install -y --no-install-recommends        \
        unzip p7zip-full file curl jq                                   \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0 liblzma5 libreadline7 \
        libncurses5 zlib1g                                              \
        libkrb5-3 libgssapi-krb5-2 krb5-user                            \
    && rm -rf /var/lib/apt/lists/*

COPY --from=base /usr/lib/x86_64-linux-gnu/libstdc++.so.${LIBSTDCXX_VERSION}  /usr/lib/x86_64-linux-gnu/
COPY --from=base /usr/lib/x86_64-linux-gnu/libgcc_s.so.1                      /lib/x86_64-linux-gnu/
COPY --from=python-slim /python/                                              /usr/local/
RUN rm -f /usr/lib/x86_64-linux-gnu/libstdc++.so.${LIBSTDCXX_OLD} \
    && ln -sf libstdc++.so.${LIBSTDCXX_VERSION} /usr/lib/x86_64-linux-gnu/libstdc++.so.6 \
    && ldconfig

RUN rm -rf /usr/share/man /usr/share/doc /usr/share/info /usr/share/locale \
           /usr/share/lintian /var/cache/* /var/log/*

RUN mkdir -p /usr/src /workspace /mnt/pgsock


# корень одного плагина поверх базы: apt и оверлей из декларации, точки монтирования.
FROM sandbox-base AS plugin-root

ARG BOBA_DIR=/app/boba
ARG SANDBOX_DEV_KRB=/app/boba/conf/krb
ARG PYTHON_VERSION=3.11
ARG JOBS=4

COPY --from=plugin-site /opt/plugin-apt.txt   /tmp/plugin-apt.txt
COPY --from=plugin-site /opt/plugin-setup.d/  /tmp/plugin-setup.d/
COPY --from=plugin-site /opt/plugin-data.txt  /tmp/plugin-data.txt

RUN if [ -s /tmp/plugin-apt.txt ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends $(tr '\n' ' ' < /tmp/plugin-apt.txt) \
        && rm -rf /var/lib/apt/lists/*; \
    fi

COPY --from=plugin-site /opt/plugin-root/  /
RUN for setup in /tmp/plugin-setup.d/*.sh; do \
        if [ -f "$setup" ]; then sh "$setup"; fi; \
    done

# чистка идёт до точек монтирования: /var/cache/* снёс бы созданный ниже
# /var/cache/fastembed, а в read-only корне bwrap его уже не создаст
RUN rm -rf /tmp/plugin-setup.d /tmp/plugin-apt.txt \
           /usr/share/man /usr/share/doc /usr/share/info /usr/share/locale \
           /usr/share/lintian /var/cache/* /var/log/*

# данные (веса эмбеддера, модели OCR) монтирует рантайм: образ несёт точки монтирования
RUN for path in $(cat /tmp/plugin-data.txt); do mkdir -p "$path"; done \
    && for path in $(cat /tmp/plugin-data.txt); do test -d "$path"; done \
    && rm -f /tmp/plugin-data.txt
RUN mkdir -p "${BOBA_DIR}/conf/krb" ${SANDBOX_DEV_KRB}

COPY --from=plugin-site /opt/site          /usr/local/lib/python${PYTHON_VERSION}/site-packages
COPY --from=plugin-site /opt/code-sandbox  /usr/src
RUN PYTHONHOME=/usr/local LD_LIBRARY_PATH=/usr/local/lib /usr/local/bin/python3 \
        -m compileall -q -j "${JOBS}" -x "/(test|tests|lib2to3|idle_test)/" \
        /usr/local/lib/python${PYTHON_VERSION} /usr/src


FROM ext4-tools AS plugin-image

COPY --from=plugin-root / /tree/
RUN used_mb=$(du -sm /tree | cut -f1) \
    && inodes=$(find /tree | wc -l) \
    && mkdir -p /out \
    && truncate -s "$((used_mb + used_mb / 20 + 16))M" /out/rootfs.ext4 \
    && /tools/mke2fs -F -q -t ext4 -O ^has_journal -m 0 -L rootfs \
        -N "$((inodes + inodes / 5 + 1000))" -d /tree /out/rootfs.ext4 \
    && chmod 0644 /out/rootfs.ext4


FROM scratch AS plugin-output

COPY --from=plugin-image /out/rootfs.ext4 /out/rootfs.ext4


FROM ${PLUGIN_IMAGE} AS plugin-input

FROM ${SANDBOX_IMAGE} AS sandbox-input

FROM sandbox-input AS sandbox-images

ARG PLUGIN

COPY --from=plugin-input /out/rootfs.ext4 /plugins/${PLUGIN}/rootfs.ext4


# --- фронт: стадии node, на хосте node не нужен -----------------------------------

FROM ${NODE_IMAGE} AS node-base

COPY --from=site-conf /certs/ /
COPY build/conf/ /tmp/conf/
RUN if [ -f /tmp/conf/npmrc ]; then \
        printf '\n' >> "$(npm config get globalconfig)"; \
        cat /tmp/conf/npmrc >> "$(npm config get globalconfig)"; \
    fi \
    && rm -rf /tmp/conf

# UI chainlit (фронт + copilot) из исходников тега с overlay web/chainlit-ui поверх;
# зависимости pnpm берёт из реестра npm образа nodejs
FROM node-base AS chainlit-ui-build

# число потоков esbuild в vite
ARG JOBS=4
ENV GOMAXPROCS=${JOBS}

ARG CHAINLIT_VERSION

COPY build/src/chainlit-${CHAINLIT_VERSION}.tar.gz  /tmp/chainlit.tar.gz
COPY packages/agents/boba-chainlit/web/chainlit-ui/overlay/  /tmp/overlay/

ENV HOME=/tmp HUSKY=0 CYPRESS_INSTALL_BINARY=0
WORKDIR /tmp/chainlit-ui
RUN root=$(tar -tzf /tmp/chainlit.tar.gz | head -1 | cut -d/ -f1) \
    && tar -xzf /tmp/chainlit.tar.gz --strip-components=1 \
        "$root/frontend" "$root/libs" "$root/package.json" "$root/pnpm-lock.yaml" \
        "$root/pnpm-workspace.yaml" "$root/.npmrc" \
    && cp -a /tmp/overlay/. .
RUN pnpm install --store-dir /tmp/pnpm-store --frozen-lockfile --config.package-manager-strict=false \
        --filter @chainlit/react-client --filter @chainlit/app --filter @chainlit/copilot \
    && pnpm --filter @chainlit/react-client run build \
    && pnpm --filter @chainlit/app run build \
    && pnpm --filter @chainlit/copilot run build \
    && sed -i -e '/rel="preconnect"/d' \
        -e 's#https://fonts.googleapis.com/css2?family=Inter[^"]*#/public/vendor/inter/inter.css#' \
        -e 's#https://cdn.jsdelivr.net/npm/katex@[0-9.]*/dist/katex.min.css#/public/vendor/katex/katex.min.css#' \
        frontend/dist/index.html \
    && mkdir -p /out/ui \
    && cp -a frontend/dist/. /out/ui/ \
    && cp -a libs/copilot/dist/. /out/ui/ \
    && rm -rf /tmp/chainlit-ui /tmp/pnpm-store


# скрипт страницы chainlit (web/page, TypeScript)
FROM node-base AS web-build

# число потоков esbuild в vite
ARG JOBS=4
ENV GOMAXPROCS=${JOBS}

COPY packages/agents/boba-chainlit/web/page/  /src/web/page/

WORKDIR /src/web/page
RUN npm ci --no-audit --no-fund && npm run build


# site-packages приложения: ставится по своему графу зависимостей, парсеры и
# эмбеддер сюда не попадают — они объявлены как extra payload у tool-пакетов
# стоит после стадий плагинов: классический builder проходит все стадии до --target,
# и make plugin-rootfs не должен ради корня плагина ставить всё приложение
FROM base AS app-site

ARG APP_PACKAGES="boba-chainlit boba-llm-http boba-llm-onnx[generation] boba-llm-fastembed"

COPY --from=boba-wheels /tmp/wheels/  /tmp/wheels/

RUN uv pip install --python "$(command -v python3)" --target /opt/app-site \
        --constraint /tmp/wheels/constraints.txt --find-links=/tmp/wheels \
        ${APP_PACKAGES}

ENV PYTHONPATH=/opt/app-site


FROM ${GLIBC_IMAGE} AS runtime-base

ARG PYTHON_VERSION=3.11
ARG LIBSTDCXX_VERSION=6.0.25
ARG BOBA_DIR=/app/boba
ARG APP_PORT=8501
ARG APP_PREFIX=/boba
ARG HEALTH_PATH=/

RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0                \
        liblzma5 libreadline7 libncurses5 zlib1g                 \
        libkrb5-3 libgssapi-krb5-2 krb5-user                     \
    && rm -rf /var/lib/apt/lists/*

COPY --from=site-conf /certs/                                                 /
COPY --from=python-slim /python/                                              ${BOBA_DIR}/third/
COPY --from=base /usr/lib/x86_64-linux-gnu/libstdc++.so.6                     ${BOBA_DIR}/third/lib/
COPY --from=base /usr/lib/x86_64-linux-gnu/libstdc++.so.${LIBSTDCXX_VERSION}  ${BOBA_DIR}/third/lib/
COPY --from=base /usr/lib/x86_64-linux-gnu/libgcc_s.so.1                      ${BOBA_DIR}/third/lib/
COPY --from=bwrap-build /out/bwrap                                            ${BOBA_DIR}/third/bin/bwrap
COPY --from=fuse2fs-build /out/fuse2fs                                        ${BOBA_DIR}/third/bin/fuse2fs
COPY --from=app-site /opt/app-site                                            ${BOBA_DIR}/app/

RUN mkdir -p ${BOBA_DIR}/conf ${BOBA_DIR}/app_root/public ${BOBA_DIR}/sandbox

# шебанги стадии сборки ведут на /usr/local/bin/python3, которого здесь нет
RUN sed -i "1s|^#!.*python3.*|#!${BOBA_DIR}/third/bin/python3|" ${BOBA_DIR}/app/bin/*

# TrustedBinaries не примет third/bin, доступный на запись посторонним
RUN chmod -R go-w ${BOBA_DIR}/third ${BOBA_DIR}/sandbox

WORKDIR /app
ENV BOBA_DIR=${BOBA_DIR} BOBA_PORT=${APP_PORT} BOBA_URL_PREFIX=${APP_PREFIX} \
    BOBA_HEALTH_PATH=${HEALTH_PATH} PYTHONHOME=${BOBA_DIR}/third PYTHONPATH=${BOBA_DIR}/app \
    LD_LIBRARY_PATH=${BOBA_DIR}/third/lib \
    PATH=${BOBA_DIR}/app/bin:${BOBA_DIR}/third/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# boba.env может переопределить порт и префикс HTTP-пробы.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python3 -c 'import os, urllib.request; e = os.environ; urllib.request.urlopen("http://127.0.0.1:" + e["BOBA_PORT"] + e["BOBA_URL_PREFIX"] + e["BOBA_HEALTH_PATH"], timeout=4).close()'

ENTRYPOINT ["python3"]


# образ чата: ассеты chainlit, vendor-шрифты и стили формул вместо CDN, локальная
# модель переформулировщика. Вложения сессий chainlit пишет в [chainlit].files_dir
# (каталог данных), app_root остаётся статикой
FROM runtime-base AS runtime-chainlit

ARG BOBA_DIR=/app/boba
ARG BOBA_UID=1000
ARG BOBA_GID=1000

COPY --chown=${BOBA_UID}:${BOBA_GID} packages/agents/boba-chainlit/assets/.chainlit/config.toml   ${BOBA_DIR}/app_root/.chainlit/
COPY --chown=${BOBA_UID}:${BOBA_GID} packages/agents/boba-chainlit/assets/.chainlit/translations/ ${BOBA_DIR}/app_root/.chainlit/translations/
COPY --chown=${BOBA_UID}:${BOBA_GID} packages/agents/boba-chainlit/assets/chainlit.md             ${BOBA_DIR}/app_root/
COPY --chown=${BOBA_UID}:${BOBA_GID} packages/agents/boba-chainlit/assets/public/                 ${BOBA_DIR}/app_root/public/
COPY --chown=${BOBA_UID}:${BOBA_GID} --from=web-build /src/assets/public/page.js                  ${BOBA_DIR}/app_root/public/page.js
COPY --chown=${BOBA_UID}:${BOBA_GID} --from=chainlit-ui-build /out/ui/                            ${BOBA_DIR}/app_root/ui/
COPY --chown=${BOBA_UID}:${BOBA_GID} build/src/webassets/                                ${BOBA_DIR}/app_root/public/vendor/

# файлы уже принадлежат пользователю, каталогам владелец ставится без рекурсии
RUN chown ${BOBA_UID}:${BOBA_GID} ${BOBA_DIR}/app_root \
       ${BOBA_DIR}/app_root/.chainlit ${BOBA_DIR}/app_root/public

# Собственные ext4-файлы каждого образа: rebuild не меняет файлы живых контейнеров.
COPY --from=sandbox-input /plugins/                 ${BOBA_DIR}/sandbox/plugins/
COPY --from=workspace-image /out/workspace.ext4     ${BOBA_DIR}/sandbox/workspace.ext4

EXPOSE 8501
USER ${BOBA_UID}:${BOBA_GID}
CMD ["-m", "boba.chainlit.main", "--config", "/app/boba/conf/config.toml"]
