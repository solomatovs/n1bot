ARG PYTHON_VERSION=3.11
ARG GLIBC_IMAGE=dmp/glibc:2.28
ARG GCC_IMAGE=dmp/gcc:8.5.0
ARG PYTHON_IMAGE=dmp/python:${PYTHON_VERSION}-dist

FROM ${PYTHON_IMAGE} AS python-dist
FROM ${GCC_IMAGE} AS base


RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0                \
        liblzma5 libreadline7 libncurses5 zlib1g                 \
        libkrb5-dev                                              \
    && rm -rf /var/lib/apt/lists/*

# перекрываем системный python своим
COPY --from=python-dist /opt/python /
RUN ldconfig                    \
    && gcc --version | head -n1 \
    && python3 --version        \
    && pip3 --version

# у каждой программы свой способ узнать CA bundle
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt        \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt   \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt             \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt       \
    GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt

# индексы пакетов и CA закрытого контура; файлов нет - идём в pypi.org
COPY build/conf/  /tmp/conf/

RUN set -eu                                                        \
    && if [ ! -f /tmp/conf/ca-chain.crt ]; then                    \
           exit 0;                                                 \
       fi                                                          \
    && mkdir -p /usr/local/share/ca-certificates                   \
    && cp /tmp/conf/ca-chain.crt /usr/local/share/ca-certificates/ \
    && update-ca-certificates

RUN set -eu                                      \
    && if [ -f /tmp/conf/pip.conf ]; then        \
           cp /tmp/conf/pip.conf /etc/pip.conf;  \
       fi                                        \
    && if [ -f /tmp/conf/uv.toml ]; then         \
           mkdir -p /etc/uv;                     \
           cp /tmp/conf/uv.toml /etc/uv/uv.toml; \
       fi                                        \
    && rm -rf /tmp/conf
COPY build/src/uv/uv  /usr/local/bin/uv


# bubblewrap building
FROM base AS bwrap-build

ARG BWRAP_VERSION=0.11.2

RUN apt-get update && apt-get install -y --no-install-recommends \
        libcap-dev meson ninja-build pkg-config                  \
        xz-utils                                                 \
    && rm -rf /var/lib/apt/lists/*

COPY build/src/bubblewrap-${BWRAP_VERSION}.tar.xz  /tmp/bwrap.tar.xz

RUN set -eu                                                                                 \
    && mkdir -p /tmp/bwrap-src && cd /tmp/bwrap-src                                         \
    && tar -xJf /tmp/bwrap.tar.xz --strip-components=1                                      \
    && meson setup _build                                                                   \
        --buildtype=release --prefix=/usr                                                   \
        -Dselinux=disabled -Dman=disabled                                                   \
    && sed -i 's|/usr/lib/x86_64-linux-gnu/libcap\.so|/usr/lib/x86_64-linux-gnu/libcap.a|g' \
        _build/build.ninja                                                                  \
    && ninja -C _build bwrap                                                                \
    && strip _build/bwrap                                                                   \
    && install -D -m 0755 _build/bwrap /out/bwrap

RUN set -eu                                     \
    && if ldd /out/bwrap | grep -q libcap; then \
           echo "libcap не статическая" >&2;    \
           exit 1;                              \
       fi                                       \
    && /out/bwrap --version


# fuse2fs and workspace.ext4 building
FROM base AS fuse2fs-build

ARG LIBFUSE_VERSION=3.12.0
ARG E2FSPROGS_VERSION=1.47.2
ARG SANDBOX_WS_SIZE=1G

RUN apt-get update && apt-get install -y --no-install-recommends \
        meson ninja-build pkg-config xz-utils                    \
    && rm -rf /var/lib/apt/lists/*

COPY build/src/fuse-${LIBFUSE_VERSION}.tar.xz         /tmp/fuse.tar.xz
COPY build/src/e2fsprogs-${E2FSPROGS_VERSION}.tar.xz  /tmp/e2fsprogs.tar.xz

RUN set -eu                                                                   \
    && mkdir -p /tmp/fuse-src && cd /tmp/fuse-src                             \
    && tar -xJf /tmp/fuse.tar.xz --strip-components=1                         \
    && meson setup _build                                                     \
        --buildtype=release --prefix=/usr/local                               \
        -Ddefault_library=static -Dexamples=false -Dtests=false -Dutils=false \
    && ninja -C _build install

RUN set -eu                                                                                      \
    && mkdir -p /tmp/e2fs-src && cd /tmp/e2fs-src                                                \
    && tar -xJf /tmp/e2fsprogs.tar.xz --strip-components=1                                       \
    && export PKG_CONFIG_PATH=/usr/local/lib/pkgconfig:/usr/local/lib/x86_64-linux-gnu/pkgconfig \
    && LIBS="-lpthread -ldl" ./configure --enable-fuse2fs --disable-nls                          \
    && make -j"$(nproc)"                                                                         \
    && strip misc/fuse2fs                                                                        \
    && install -D -m 0755 misc/fuse2fs /out/fuse2fs

RUN set -eu                                     \
    && if ldd /out/fuse2fs | grep -q fuse; then \
           echo "libfuse не статическая" >&2;   \
           exit 1;                              \
       fi                                       \
    && /out/fuse2fs -V

# шаблон workspace делает тот же mke2fs, что уедет в релиз
RUN set -eu                                                           \
    && cd /tmp/e2fs-src                                               \
    && truncate -s ${SANDBOX_WS_SIZE} /out/sandbox-workspace.ext4     \
    && MKE2FS_CONFIG=/tmp/e2fs-src/misc/mke2fs.conf                   \
        ./misc/mke2fs -F -q -t ext4 -O ^has_journal -m 0 -L workspace \
        /out/sandbox-workspace.ext4


# sandbox-rootfs prepare
FROM ${GLIBC_IMAGE} AS sandbox-rootfs

ARG PYTHON_VERSION=3.11
ARG LIBSTDCXX_VERSION=6.0.25
ARG LIBSTDCXX_OLD=6.0.22
ARG LITEPARSE_PACKAGES="imagemagick libreoffice-writer libreoffice-calc libreoffice-impress ghostscript fonts-dejavu-core"

COPY build/conf/                                  /tmp/conf/

RUN set -eu                                                        \
    && if [ ! -f /tmp/conf/ca-chain.crt ]; then                    \
           exit 0;                                                 \
       fi                                                          \
    && mkdir -p /usr/local/share/ca-certificates                   \
    && cp /tmp/conf/ca-chain.crt /usr/local/share/ca-certificates/ \
    && update-ca-certificates

RUN set -eu                                                             \
    && apt-get update && apt-get install -y --no-install-recommends     \
        unzip p7zip-full file curl jq                                   \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0 liblzma5 libreadline7 \
        libncurses5 zlib1g                                              \
        libkrb5-3 libgssapi-krb5-2 krb5-user                            \
        ${LITEPARSE_PACKAGES}                                           \
    && rm -rf /var/lib/apt/lists/* /tmp/conf

# рантайм C++ в образе ровно один — тулчейновый. Дистрибутивный libstdc++ даёт
# только CXXABI_1.3.10, а колёса (onnxruntime) требуют 1.3.11; пока обе копии
# лежали в путях загрузчика, выбор решал порядок ld.so.cache и на разных
# машинах давал разный результат. Ставится после apt: установка пакетов
# принесла бы дистрибутивную версию обратно
COPY --from=base /usr/local/lib64/libstdc++.so.${LIBSTDCXX_VERSION}   \
                 /usr/lib/x86_64-linux-gnu/
COPY --from=base /usr/local/lib64/libgcc_s.so.1                       \
                 /lib/x86_64-linux-gnu/

RUN set -eu                                                          \
    && rm -f /usr/lib/x86_64-linux-gnu/libstdc++.so.${LIBSTDCXX_OLD} \
    && ln -sf libstdc++.so.${LIBSTDCXX_VERSION}                      \
        /usr/lib/x86_64-linux-gnu/libstdc++.so.6                     \
    && ldconfig

# liteparse зовёт единый magick, apt даёт только утилиты ImageMagick 6
RUN set -eu                                                                             \
    && printf '%s\n'                                                                    \
        '#!/bin/sh'                                                                     \
        'case "$1" in'                                                                  \
        '    convert|identify|mogrify|composite|montage|compare|stream|conjure|import)' \
        '        cmd="$1"'                                                              \
        '        shift'                                                                 \
        '        exec "$cmd" "$@"'                                                      \
        '        ;;'                                                                    \
        'esac'                                                                          \
        'exec convert "$@"'                                                             \
        > /usr/local/bin/magick                                                         \
    && chmod 0755 /usr/local/bin/magick

# ImageMagick по умолчанию запрещает читать PDF
RUN set -eu                                                                                                \
    && if grep -q 'pattern="PDF"' /etc/ImageMagick-6/policy.xml; then                                      \
           exit 0;                                                                                         \
       fi                                                                                                  \
    && sed -i 's#</policymap>#  <policy domain="coder" rights="read|write" pattern="PDF"/>\n</policymap>#' \
           /etc/ImageMagick-6/policy.xml

# каталоги под содержимое образа песочницы и точки монтирования: bwrap не
# создаёт их в read-only корне. python лежит в /usr/local (он собран с этим
# prefix), site-packages штатные — PYTHONHOME и PYTHONPATH внутри не нужны
RUN mkdir -p /usr/local/lib/python${PYTHON_VERSION}/site-packages \
             /usr/src /usr/share/tessdata /var/cache/fastembed    \
             /workspace /mnt/pgsock

RUN set -eu                                                                 \
    && for tool in unzip 7z file head curl jq convert magick soffice gs; do \
        command -v "$tool" > /dev/null;                                     \
    done


# boba wheels
FROM base AS boba-wheels

COPY pyproject.toml  /tmp/boba/pyproject.toml
COPY packages/       /tmp/boba/packages/

RUN set -eu         \
    && cd /tmp/boba \
    && uv build --all-packages --wheel --no-sources -o /tmp/wheels

# boba site-packages
FROM base AS app-site

COPY --from=boba-wheels /tmp/wheels/  /tmp/wheels/

# приложение ставится по своему графу зависимостей: парсеры и эмбеддер
# сюда не попадают — они объявлены как extra payload у tool-пакетов
RUN set -eu                                                                   \
    && uv pip install --python "$(command -v python3)" --target /opt/app-site \
        --find-links=/tmp/wheels boba-chainlit                                \
    && rm -rf /tmp/wheels

# фронт chainlit тянет шрифт и стили формул с CDN: в закрытом контуре их отдаёт
# сам сервер из public/, куда make webassets кладёт katex и Inter
RUN set -eu                                                                                                      \
    && index=/opt/app-site/chainlit/frontend/dist/index.html                                                     \
    && sed -i                                                                                                    \
        -e '/rel="preconnect"/d'                                                                                 \
        -e 's#https://fonts.googleapis.com/css2?family=Inter[^"]*#/public/vendor/inter/inter.css#'               \
        -e 's#https://cdn.jsdelivr.net/npm/katex@[0-9.]*/dist/katex.min.css#/public/vendor/katex/katex.min.css#' \
        "$index"                                                                                                 \
    && if grep -qE 'fonts\.(googleapis|gstatic)\.com|cdn\.jsdelivr\.net' "$index"; then                          \
           echo "во фронте остались ссылки на CDN" >&2;                                                          \
           exit 1;                                                                                               \
       fi

RUN set -eu                                                            \
    && export PYTHONPATH=/opt/app-site                                 \
    && python3 -c "import boba.chainlit; print('app-site ok')"         \
    && python3 -c "import importlib.metadata as md;                    \
print('\\n'.join(sorted(d.metadata['Name'] for d in md.distributions() \
if d.metadata['Name'].startswith('boba-'))))" > /opt/app-packages.txt


# site-packages песочницы
FROM base AS sandbox-site

COPY --from=boba-wheels /tmp/wheels/  /tmp/wheels/
COPY build/src/names-sandbox.txt      /tmp/names-sandbox.txt

# в песочнице живут только инструменты: приложение сюда не ставится
RUN set -eu                                                               \
    && cat /tmp/names-sandbox.txt                                         \
    && uv pip install --python "$(command -v python3)" --target /opt/site \
        --find-links=/tmp/wheels $(cat /tmp/names-sandbox.txt)            \
    && rm -rf /tmp/wheels

RUN PYTHONPATH=/opt/site python3 -c "import liteparse, markdownify, bs4, lxml, plotly, httpx, psycopg, clickhouse_connect, aiohttp, fastembed, gssapi, krb5, pandas, openpyxl; print('site ok')"

# код инструментов уезжает отдельно: он запекается в /usr/src образа песочницы,
# а sys.path сюда добавляет boba-src.pth рядом с site-packages
RUN set -eu                                             \
    && mkdir -p /opt/code-sandbox/boba                  \
    && mv /opt/site/boba/* /opt/code-sandbox/boba/      \
    && rmdir /opt/site/boba                             \
    && mv /opt/site/boba_*.dist-info /opt/code-sandbox/ \
    && echo /usr/src > /opt/site/boba-src.pth           \
    && PYTHONPATH=/opt/code-sandbox:/opt/site python3 -c "import boba.tool; print('code-sandbox ok')"


# образ корня песочницы: собранное дерево одним ext4-файлом. Тем же mke2fs,
# что делает шаблон workspace, иначе это были бы два разных образа.
FROM fuse2fs-build AS sandbox-image

ARG PYTHON_VERSION=3.11
ARG BOBA_DIR=/app/boba
ARG SANDBOX_DEV_KRB=/app/boba/conf/krb

COPY --from=sandbox-rootfs /                   /tree/
COPY --from=python-dist /opt/python/usr/local  /tree/usr/local/
COPY --from=sandbox-site /opt/site             /tree/usr/local/lib/python${PYTHON_VERSION}/site-packages/
COPY --from=sandbox-site /opt/code-sandbox     /tree/usr/src/
COPY build/src/tessdata/                       /tree/usr/share/tessdata/
COPY build/src/fastembed/                      /tree/var/cache/fastembed/

# точки монтирования на read-only корне bwrap не создаёт: и путь релиза,
# и путь дерева исходников заводим заранее
RUN set -eu                                                           \
    && mkdir -p "/tree${BOBA_DIR}/conf/krb" "/tree${SANDBOX_DEV_KRB}" \
    && cd /tree/usr/share/tessdata                                    \
    && ln -sf eng.traineddata rus+eng.traineddata                     \
    && ln -sf rus.traineddata eng+rus.traineddata                     \
    && cd /tmp/e2fs-src                                               \
    && used_mb=$(du -sm /tree | cut -f1)                              \
    && size_mb=$(( used_mb + used_mb / 5 + 512 ))                     \
    && inodes=$(find /tree | wc -l)                                   \
    && truncate -s ${size_mb}M /out/sandbox-rootfs.ext4               \
    && MKE2FS_CONFIG=/tmp/e2fs-src/misc/mke2fs.conf                   \
        ./misc/mke2fs -F -q -t ext4 -O ^has_journal -m 0              \
        -L rootfs -N $(( inodes + inodes / 5 + 1000 ))                \
        -d /tree /out/sandbox-rootfs.ext4                             \
    && ls -lh /out/sandbox-rootfs.ext4


# runtime image for docker-compose
# and make extract copy to server host
FROM ${GLIBC_IMAGE} AS runtime

ARG PYTHON_VERSION=3.11   \
    BOBA_DIR=/app/boba    \
    BOBA_VERSION=0.0.0    \
    BOBA_UID=1000         \
    BOBA_GID=1000


RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0                \
        liblzma5 libreadline7 libncurses5 zlib1g                 \
        libkrb5-3 libgssapi-krb5-2 krb5-user                     \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python-dist /opt/python/usr/local          ${BOBA_DIR}/third
COPY --from=base /usr/local/lib64/libstdc++.so.6       ${BOBA_DIR}/third/lib/
COPY --from=base /usr/local/lib64/libstdc++.so.6.0.25  ${BOBA_DIR}/third/lib/
COPY --from=base /usr/local/bin/uv                     ${BOBA_DIR}/third/bin/uv
COPY --from=bwrap-build /out/bwrap                     ${BOBA_DIR}/third/bin/bwrap
COPY --from=fuse2fs-build /out/fuse2fs                 ${BOBA_DIR}/third/bin/fuse2fs
COPY --from=sandbox-image /out/sandbox-rootfs.ext4      ${BOBA_DIR}/sandbox/rootfs.ext4
COPY --from=fuse2fs-build /out/sandbox-workspace.ext4  ${BOBA_DIR}/sandbox/workspace.ext4
COPY build/conf/                                       /tmp/conf/

COPY --from=app-site /opt/app-site          ${BOBA_DIR}/app/
COPY --from=app-site /opt/app-packages.txt  ${BOBA_DIR}/app/packages.txt

COPY packages/agents/boba-chainlit/assets/chainlit/config.toml    ${BOBA_DIR}/app_root/.chainlit/
COPY packages/agents/boba-chainlit/assets/chainlit/translations/  ${BOBA_DIR}/app_root/.chainlit/translations/
COPY packages/agents/boba-chainlit/assets/chainlit/chainlit.md    ${BOBA_DIR}/app_root/
COPY packages/agents/boba-chainlit/assets/public/                 ${BOBA_DIR}/app_root/public/
COPY build/src/webassets/                                         ${BOBA_DIR}/app_root/public/vendor/

RUN set -eu                                                        \
    && if [ ! -f /tmp/conf/ca-chain.crt ]; then                    \
           exit 0;                                                 \
       fi                                                          \
    && mkdir -p /usr/local/share/ca-certificates                   \
    && cp /tmp/conf/ca-chain.crt /usr/local/share/ca-certificates/ \
    && update-ca-certificates

# .files создаётся заранее: chainlit делает mkdir на импорте, ассеты read-only.
# Каталога data в образе нет: данные переживают релизы и лежат в BOBA_DATA
RUN mkdir -p ${BOBA_DIR}/conf \
             ${BOBA_DIR}/app_root/.files

# шебанги стадии сборки ведут на /usr/local/bin/python3, которого здесь нет
RUN sed -i "1s|^#!.*python3.*|#!${BOBA_DIR}/third/bin/python3|" \
        ${BOBA_DIR}/app/bin/*

RUN chown -R ${BOBA_UID}:${BOBA_GID} ${BOBA_DIR}/app_root

RUN set -eu                                                                               \
    && chmod -R go-w ${BOBA_DIR}/third ${BOBA_DIR}/sandbox                                \
    && writable=$(find ${BOBA_DIR}/third/bin -maxdepth 1 -type f -perm /022 -print -quit) \
    && if [ -n "$writable" ]; then                                                        \
           echo "third/bin доступен на запись посторонним: $writable" >&2;                \
           exit 1;                                                                        \
       fi                                                                                 \
    && ${BOBA_DIR}/third/bin/bwrap --version                                              \
    && ${BOBA_DIR}/third/bin/fuse2fs -V

RUN rm -rf /tmp/conf

WORKDIR /app
EXPOSE 8501

ENV BOBA_DIR=${BOBA_DIR}
USER ${BOBA_UID}:${BOBA_GID}

# порт и префикс приходят из boba.env, как и сам python: проба идёт тем же
# интерпретатором, что и приложение, чтобы не тащить в образ curl
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3   \
    CMD python3 -c "import os, urllib.request;                           \
url = 'http://127.0.0.1:' + os.environ['BOBA_PORT'] + os.environ['BOBA_URL_PREFIX'] + '/'; \
urllib.request.urlopen(url, timeout=4)"

ENTRYPOINT ["python3"]
CMD ["-m", "boba.chainlit.main"]
