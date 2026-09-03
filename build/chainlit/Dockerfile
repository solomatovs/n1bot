ARG PYTHON_VERSION=3.11
ARG GLIBC_IMAGE=dmp/glibc:2.28
ARG GCC_IMAGE=dmp/gcc:8.5.0
ARG PYTHON_IMAGE=dmp/python:${PYTHON_VERSION}-dist
ARG NODE_IMAGE=dmp/nodejs:20

FROM ${PYTHON_IMAGE} AS python-dist


# закрытый контур: CA внутреннего TLS и индексы пакетов из build/conf/common.
# Стадии берут готовые деревья: /certs — всем, /index — только сборочным
FROM ${GLIBC_IMAGE} AS site-conf

COPY build/chainlit/conf/  /tmp/conf/

RUN mkdir -p /certs/etc/ssl /certs/usr/local/share /index/etc/uv        \
    && if [ -f /tmp/conf/ca-chain.crt ]; then                           \
           cp /tmp/conf/ca-chain.crt /usr/local/share/ca-certificates/  \
           && update-ca-certificates;                                   \
       fi                                                               \
    && cp -a /etc/ssl/certs /certs/etc/ssl/                             \
    && cp -a /usr/local/share/ca-certificates /certs/usr/local/share/   \
    && if [ -f /tmp/conf/pip.conf ]; then                               \
           cp /tmp/conf/pip.conf /index/etc/pip.conf;                   \
       fi                                                               \
    && if [ -f /tmp/conf/uv.toml ]; then                                \
           cp /tmp/conf/uv.toml /index/etc/uv/uv.toml;                  \
       fi


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
COPY build/chainlit/src/uv/uv                 /usr/local/bin/uv

RUN ldconfig && gcc --version | head -n1 && python3 --version && pip3 --version

# у каждой программы свой способ узнать CA bundle
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt        \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt   \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt             \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt       \
    GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt


# bubblewrap статически с libcap: у meson bwrap нет такой опции, поэтому
# в сгенерированном build.ninja .so подменяется на .a
FROM base AS bwrap-build

ARG BWRAP_VERSION=0.11.2

RUN apt-get update && apt-get install -y --no-install-recommends \
        libcap-dev meson ninja-build pkg-config xz-utils         \
    && rm -rf /var/lib/apt/lists/*

COPY build/chainlit/src/bubblewrap-${BWRAP_VERSION}.tar.xz  /tmp/bwrap.tar.xz

WORKDIR /tmp/bwrap-src
RUN tar -xJf /tmp/bwrap.tar.xz --strip-components=1                                     \
    && meson setup _build --buildtype=release --prefix=/usr -Dselinux=disabled -Dman=disabled \
    && sed -i 's|/usr/lib/x86_64-linux-gnu/libcap\.so|/usr/lib/x86_64-linux-gnu/libcap.a|g' _build/build.ninja \
    && ninja -C _build bwrap                                                            \
    && strip _build/bwrap                                                               \
    && install -D -m 0755 _build/bwrap /out/bwrap

RUN ! ldd /out/bwrap | grep libcap && /out/bwrap --version


# fuse2fs статически с libfuse, плюс шаблон workspace тем же mke2fs, что уедет в релиз
FROM base AS fuse2fs-build

ARG LIBFUSE_VERSION=3.12.0
ARG E2FSPROGS_VERSION=1.47.2
ARG SANDBOX_WS_SIZE=1G

RUN apt-get update && apt-get install -y --no-install-recommends \
        meson ninja-build pkg-config xz-utils                    \
    && rm -rf /var/lib/apt/lists/*

COPY build/chainlit/src/fuse-${LIBFUSE_VERSION}.tar.xz         /tmp/fuse.tar.xz
COPY build/chainlit/src/e2fsprogs-${E2FSPROGS_VERSION}.tar.xz  /tmp/e2fsprogs.tar.xz

WORKDIR /tmp/fuse-src
RUN tar -xJf /tmp/fuse.tar.xz --strip-components=1                                        \
    && meson setup _build --buildtype=release --prefix=/usr/local                         \
        -Ddefault_library=static -Dexamples=false -Dtests=false -Dutils=false             \
    && ninja -C _build install

WORKDIR /tmp/e2fs-src
ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig:/usr/local/lib/x86_64-linux-gnu/pkgconfig
RUN tar -xJf /tmp/e2fsprogs.tar.xz --strip-components=1                                   \
    && LIBS="-lpthread -ldl" ./configure --enable-fuse2fs --disable-nls                   \
    && make -j"$(nproc)"                                                                  \
    && strip misc/fuse2fs                                                                 \
    && install -D -m 0755 misc/fuse2fs /out/fuse2fs

RUN ! ldd /out/fuse2fs | grep fuse && /out/fuse2fs -V

RUN truncate -s ${SANDBOX_WS_SIZE} /out/sandbox-workspace.ext4                            \
    && MKE2FS_CONFIG=/tmp/e2fs-src/misc/mke2fs.conf                                       \
       ./misc/mke2fs -F -q -t ext4 -O ^has_journal -m 0 -L workspace /out/sandbox-workspace.ext4


# колёса всех пакетов репозитория и constraints из uv.lock: обе стадии установки
# получают версии хоста, иначе uv ставил бы свежайшие
FROM base AS boba-wheels

COPY pyproject.toml  /tmp/boba/pyproject.toml
COPY uv.lock         /tmp/boba/uv.lock
COPY packages/       /tmp/boba/packages/

WORKDIR /tmp/boba
RUN uv build --all-packages --wheel --no-sources -o /tmp/wheels                        \
    && uv export --frozen --all-packages --all-extras --no-emit-workspace --no-hashes  \
        -o /tmp/wheels/constraints.txt


# site-packages приложения: ставится по своему графу зависимостей, парсеры и
# эмбеддер сюда не попадают — они объявлены как extra payload у tool-пакетов
FROM base AS app-site

ARG APP_PACKAGES="boba-chainlit boba-llm[generation]"
ARG APP_IMPORTS="boba.chainlit onnxruntime_genai"

COPY --from=boba-wheels /tmp/wheels/  /tmp/wheels/

RUN uv pip install --python "$(command -v python3)" --target /opt/app-site  \
        --constraint /tmp/wheels/constraints.txt --find-links=/tmp/wheels   \
        ${APP_PACKAGES}

ENV PYTHONPATH=/opt/app-site
RUN for name in ${APP_IMPORTS}; do python3 -c "import $name; print('$name ok')"; done
RUN pip3 list --path /opt/app-site --format=freeze | grep '^boba-' | cut -d= -f1 > /opt/app-packages.txt


# --- образ корня одного плагина: make plugin-rootfs PLUGIN=<пакет> ---------------

# site-packages плагина: закрытие payload одного пакета и его сборочные декларации
FROM base AS plugin-site

ARG PLUGIN

COPY --from=boba-wheels /tmp/wheels/         /tmp/wheels/
COPY --from=boba-wheels /tmp/boba/packages/  /tmp/packages/
COPY build/chainlit/scripts/plugin_rootfs.py    /tmp/plugin_rootfs.py

RUN test -n "${PLUGIN}"                                                              \
    && python3 /tmp/plugin_rootfs.py names /tmp/packages ${PLUGIN} > /tmp/names.txt  \
    && cat /tmp/names.txt                                                              \
    && uv pip install --python "$(command -v python3)" --target /opt/site              \
        --constraint /tmp/wheels/constraints.txt --find-links=/tmp/wheels              \
        $(cat /tmp/names.txt)

RUN python3 /tmp/plugin_rootfs.py apt /tmp/packages ${PLUGIN} > /opt/plugin-apt.txt \
    && python3 /tmp/plugin_rootfs.py data /tmp/packages ${PLUGIN} > /opt/plugin-data.txt \
    && mkdir -p /opt/plugin-root /opt/plugin-setup.d                                    \
    && for root in $(python3 /tmp/plugin_rootfs.py root /tmp/packages ${PLUGIN}); do    \
           cp -a "$root/." /opt/plugin-root/; done                                      \
    && index=0                                                                          \
    && for setup in $(python3 /tmp/plugin_rootfs.py setup /tmp/packages ${PLUGIN}); do  \
           cp "$setup" "/opt/plugin-setup.d/$index.sh"; index=$((index + 1)); done

ENV PYTHONPATH=/opt/site
RUN for name in $(python3 /tmp/plugin_rootfs.py imports /tmp/packages ${PLUGIN}); do \
        python3 -c "import $name; print('$name ok')"; done

RUN mkdir -p /opt/code-sandbox                          \
    && mv /opt/site/boba /opt/code-sandbox/boba         \
    && mv /opt/site/boba_*.dist-info /opt/code-sandbox/ \
    && echo /usr/src > /opt/site/boba-src.pth

# гость зиготы стартует в образе без биндов кода: контракт проверяется здесь
RUN PYTHONPATH=/opt/code-sandbox:/opt/site python3 -c "import boba.sandbox.guest; print('guest ok')"


# корень плагина: база песочницы плюс нативные зависимости из декларации
FROM ${GLIBC_IMAGE} AS plugin-root

ARG LIBSTDCXX_VERSION=6.0.25
ARG LIBSTDCXX_OLD=6.0.22

COPY --from=site-conf /certs/                 /
COPY --from=plugin-site /opt/plugin-apt.txt   /tmp/plugin-apt.txt
COPY --from=plugin-site /opt/plugin-setup.d/  /tmp/plugin-setup.d/

RUN apt-get update && apt-get install -y --no-install-recommends        \
        unzip p7zip-full file curl jq                                   \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0 liblzma5 libreadline7 \
        libncurses5 zlib1g                                              \
        libkrb5-3 libgssapi-krb5-2 krb5-user                            \
        $(tr '\n' ' ' < /tmp/plugin-apt.txt)                            \
    && rm -rf /var/lib/apt/lists/*

COPY --from=base /usr/lib/x86_64-linux-gnu/libstdc++.so.${LIBSTDCXX_VERSION}  /usr/lib/x86_64-linux-gnu/
COPY --from=base /usr/lib/x86_64-linux-gnu/libgcc_s.so.1                      /lib/x86_64-linux-gnu/
RUN rm -f /usr/lib/x86_64-linux-gnu/libstdc++.so.${LIBSTDCXX_OLD}                          \
    && ln -sf libstdc++.so.${LIBSTDCXX_VERSION} /usr/lib/x86_64-linux-gnu/libstdc++.so.6   \
    && ldconfig

COPY --from=plugin-site /opt/plugin-root/  /
RUN for setup in /tmp/plugin-setup.d/*.sh; do                       \
        if [ -f "$setup" ]; then sh "$setup"; fi; done               \
    && rm -rf /tmp/plugin-setup.d /tmp/plugin-apt.txt

RUN rm -rf /usr/share/man /usr/share/doc /usr/share/info /usr/share/locale \
           /usr/share/lintian /var/cache/* /var/log/*

RUN mkdir -p /usr/src /workspace /mnt/pgsock


# ext4-образ корня плагина
FROM fuse2fs-build AS plugin-rootfs

ARG PYTHON_VERSION=3.11
ARG BOBA_DIR=/app/boba
ARG SANDBOX_DEV_KRB=/app/boba/conf/krb

COPY --from=plugin-root /                      /tree/
COPY --from=python-dist /opt/python/usr/local  /tree/usr/local/
COPY --from=plugin-site /opt/site              /tree/usr/local/lib/python${PYTHON_VERSION}/site-packages/
COPY --from=plugin-site /opt/code-sandbox      /tree/usr/src/
COPY --from=plugin-site /opt/plugin-data.txt   /tmp/plugin-data.txt
COPY build/chainlit/scripts/mkrootfs.sh        /tmp/mkrootfs.sh

# статическая libpython, tests и инструменты установки телу не нужны:
# python внутри образа только исполняет модуль инструментов
# данные (веса эмбеддера, tessdata) монтирует рантайм: образ несёт точки монтирования
RUN for path in $(cat /tmp/plugin-data.txt); do mkdir -p "/tree$path"; done

RUN mkdir -p "/tree${BOBA_DIR}/conf/krb" "/tree${SANDBOX_DEV_KRB}"                        \
    && lib="/tree/usr/local/lib/python${PYTHON_VERSION}"                                  \
    && rm -rf "$lib/test" "$lib/idlelib" "$lib/tkinter" "$lib/lib2to3" "$lib/ensurepip"   \
              "$lib/config-${PYTHON_VERSION}-x86_64-linux-gnu"                            \
              "$lib/site-packages/pip" "$lib/site-packages/setuptools"                    \
              "$lib/site-packages/pkg_resources"                                          \
    && sh /tmp/mkrootfs.sh /tree /out/plugin-rootfs.ext4 ${PYTHON_VERSION} 16


FROM ${GLIBC_IMAGE} AS runtime-base

ARG PYTHON_VERSION=3.11
ARG LIBSTDCXX_VERSION=6.0.25
ARG BOBA_DIR=/app/boba
ARG HEALTH_PATH=/

RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0                \
        liblzma5 libreadline7 libncurses5 zlib1g                 \
        libkrb5-3 libgssapi-krb5-2 krb5-user                     \
    && rm -rf /var/lib/apt/lists/*

COPY --from=site-conf /certs/                                                 /
COPY --from=python-dist /opt/python/usr/local                                 ${BOBA_DIR}/third
COPY --from=base /usr/lib/x86_64-linux-gnu/libstdc++.so.6                     ${BOBA_DIR}/third/lib/
COPY --from=base /usr/lib/x86_64-linux-gnu/libstdc++.so.${LIBSTDCXX_VERSION}  ${BOBA_DIR}/third/lib/
COPY --from=base /usr/local/bin/uv                                            ${BOBA_DIR}/third/bin/uv
COPY --from=bwrap-build /out/bwrap                                            ${BOBA_DIR}/third/bin/bwrap
COPY --from=fuse2fs-build /out/fuse2fs                                        ${BOBA_DIR}/third/bin/fuse2fs
COPY --from=app-site /opt/app-site                                            ${BOBA_DIR}/app/
COPY --from=app-site /opt/app-packages.txt                                    ${BOBA_DIR}/app/packages.txt

RUN mkdir -p ${BOBA_DIR}/conf ${BOBA_DIR}/app_root/public

# шебанги стадии сборки ведут на /usr/local/bin/python3, которого здесь нет
RUN sed -i "1s|^#!.*python3.*|#!${BOBA_DIR}/third/bin/python3|" ${BOBA_DIR}/app/bin/*

# TrustedBinaries не примет third/bin, доступный на запись посторонним
RUN mkdir -p ${BOBA_DIR}/sandbox
RUN chmod -R go-w ${BOBA_DIR}/third ${BOBA_DIR}/sandbox                                   \
    && test -z "$(find ${BOBA_DIR}/third/bin -maxdepth 1 -type f -perm /022)"             \
    && ${BOBA_DIR}/third/bin/bwrap --version                                              \
    && ${BOBA_DIR}/third/bin/fuse2fs -V

WORKDIR /app
ENV BOBA_DIR=${BOBA_DIR} \
    BOBA_HEALTH_PATH=${HEALTH_PATH}

# порт, префикс и сам python приходят из boba.env: проба идёт тем же интерпретатором
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3   \
    CMD python3 -c "import os, urllib.request;                           \
url = 'http://127.0.0.1:' + os.environ['BOBA_PORT'] + os.environ['BOBA_URL_PREFIX'] + os.environ['BOBA_HEALTH_PATH']; \
urllib.request.urlopen(url, timeout=4)"

ENTRYPOINT ["python3"]


# скрипт страницы chainlit (web/page, TypeScript): собирается node'ом стадии, на хосте node не нужен
FROM ${NODE_IMAGE} AS web-build

COPY packages/agents/boba-chainlit/web/page/  /src/web/page/
WORKDIR /src/web/page
RUN npm ci --no-audit --no-fund && npm run check && npm run build


# образ чата: ассеты chainlit, vendor-шрифты и стили формул вместо CDN, локальная
# модель переформулировщика. .files создаётся заранее: chainlit делает mkdir на импорте
FROM runtime-base AS runtime-chainlit

ARG BOBA_DIR=/app/boba
ARG BOBA_UID=1000
ARG BOBA_GID=1000

COPY packages/agents/boba-chainlit/assets/.chainlit/config.toml   ${BOBA_DIR}/app_root/.chainlit/
COPY packages/agents/boba-chainlit/assets/.chainlit/translations/ ${BOBA_DIR}/app_root/.chainlit/translations/
COPY packages/agents/boba-chainlit/assets/chainlit.md             ${BOBA_DIR}/app_root/
COPY packages/agents/boba-chainlit/assets/public/                 ${BOBA_DIR}/app_root/public/
COPY --from=web-build /src/assets/public/page.js                  ${BOBA_DIR}/app_root/public/page.js
COPY build/chainlit/src/webassets/                                ${BOBA_DIR}/app_root/public/vendor/

# фронт chainlit тянет шрифт и стили формул с CDN: переводим на public/vendor
RUN index=${BOBA_DIR}/app/chainlit/frontend/dist/index.html                                                  \
    && sed -i                                                                                                \
        -e '/rel="preconnect"/d'                                                                             \
        -e 's#https://fonts.googleapis.com/css2?family=Inter[^"]*#/public/vendor/inter/inter.css#'           \
        -e 's#https://cdn.jsdelivr.net/npm/katex@[0-9.]*/dist/katex.min.css#/public/vendor/katex/katex.min.css#' \
        "$index"                                                                                             \
    && ! grep -E 'fonts\.(googleapis|gstatic)\.com|cdn\.jsdelivr\.net' "$index"

RUN mkdir -p ${BOBA_DIR}/app_root/.files && chown -R ${BOBA_UID}:${BOBA_GID} ${BOBA_DIR}/app_root

EXPOSE 8501
USER ${BOBA_UID}:${BOBA_GID}
CMD ["-m", "boba.chainlit.main", "--config", "/app/boba/conf/config.toml"]
