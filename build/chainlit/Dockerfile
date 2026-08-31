ARG PYTHON_VERSION=3.11
ARG GLIBC_IMAGE=dmp/glibc:2.28
ARG GCC_IMAGE=dmp/gcc:8.5.0
ARG PYTHON_IMAGE=dmp/python:${PYTHON_VERSION}-dist

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


# корень песочницы: нативное окружение инструментов без python и site-packages
FROM ${GLIBC_IMAGE} AS sandbox-rootfs

ARG PYTHON_VERSION=3.11
ARG LIBSTDCXX_VERSION=6.0.25
ARG LIBSTDCXX_OLD=6.0.22
ARG LITEPARSE_PACKAGES="imagemagick libreoffice-writer libreoffice-calc libreoffice-impress ghostscript fonts-dejavu-core"

COPY --from=site-conf /certs/  /

RUN apt-get update && apt-get install -y --no-install-recommends        \
        unzip p7zip-full file curl jq                                   \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0 liblzma5 libreadline7 \
        libncurses5 zlib1g                                              \
        libkrb5-3 libgssapi-krb5-2 krb5-user                            \
        ${LITEPARSE_PACKAGES}                                           \
    && rm -rf /var/lib/apt/lists/*

# рантайм C++ ровно один — тулчейновый: дистрибутивный даёт только CXXABI_1.3.10,
# а колёсам (onnxruntime) нужен 1.3.11. Ставится после apt, иначе apt вернул бы свой
COPY --from=base /usr/lib/x86_64-linux-gnu/libstdc++.so.${LIBSTDCXX_VERSION}  /usr/lib/x86_64-linux-gnu/
COPY --from=base /usr/lib/x86_64-linux-gnu/libgcc_s.so.1                      /lib/x86_64-linux-gnu/
RUN rm -f /usr/lib/x86_64-linux-gnu/libstdc++.so.${LIBSTDCXX_OLD}                          \
    && ln -sf libstdc++.so.${LIBSTDCXX_VERSION} /usr/lib/x86_64-linux-gnu/libstdc++.so.6   \
    && ldconfig

# обёртка magick для liteparse; ImageMagick по умолчанию запрещает читать PDF
COPY build/chainlit/rootfs/  /
RUN sed -i 's#</policymap>#  <policy domain="coder" rights="read|write" pattern="PDF"/>\n</policymap>#' \
        /etc/ImageMagick-6/policy.xml

# точки монтирования: bwrap не создаёт их в read-only корне. python лежит в /usr/local
# (собран с этим prefix), поэтому PYTHONHOME и PYTHONPATH внутри не нужны
RUN mkdir -p /usr/local/lib/python${PYTHON_VERSION}/site-packages \
             /usr/src /usr/share/tessdata /var/cache/fastembed    \
             /workspace /mnt/pgsock

RUN for tool in unzip 7z file head curl jq convert magick soffice gs; do command -v "$tool"; done


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


# site-packages песочницы: закрытие зависимостей приложения даёт список пакетов
# с payload, в песочнице живут только они. Код инструментов уезжает отдельно —
# в /usr/src образа корня, а sys.path добавляет boba-src.pth
FROM base AS sandbox-site

ARG APP=chainlit
ARG APP_EXTRAS=""

COPY --from=boba-wheels /tmp/wheels/         /tmp/wheels/
COPY --from=boba-wheels /tmp/boba/packages/  /tmp/packages/
COPY build/chainlit/scripts/sandbox_names.py          /tmp/sandbox_names.py

RUN python3 /tmp/sandbox_names.py /tmp/packages ${APP} ${APP_EXTRAS} > /tmp/names.txt \
    && cat /tmp/names.txt                                                              \
    && uv pip install --python "$(command -v python3)" --target /opt/site              \
        --constraint /tmp/wheels/constraints.txt --find-links=/tmp/wheels              \
        $(cat /tmp/names.txt)

RUN mkdir -p /opt/code-sandbox                    \
    && mv /opt/site/boba /opt/code-sandbox/boba   \
    && mv /opt/site/boba_*.dist-info /opt/code-sandbox/ \
    && echo /usr/src > /opt/site/boba-src.pth

ENV PYTHONPATH=/opt/code-sandbox:/opt/site
RUN python3 -c "import liteparse, markdownify, bs4, lxml, plotly, httpx, psycopg, clickhouse_connect, aiohttp, fastembed, gssapi, krb5, pandas, openpyxl, boba.tool; print('site ok')"


# образ корня песочницы: собранное дерево одним ext4-файлом
FROM fuse2fs-build AS sandbox-image

ARG PYTHON_VERSION=3.11
ARG BOBA_DIR=/app/boba
ARG SANDBOX_DEV_KRB=/app/boba/conf/krb

COPY --from=sandbox-rootfs /                   /tree/
COPY --from=python-dist /opt/python/usr/local  /tree/usr/local/
COPY --from=sandbox-site /opt/site             /tree/usr/local/lib/python${PYTHON_VERSION}/site-packages/
COPY --from=sandbox-site /opt/code-sandbox     /tree/usr/src/
COPY build/chainlit/src/tessdata/                       /tree/usr/share/tessdata/
COPY build/chainlit/scripts/mkrootfs.sh                 /tmp/mkrootfs.sh

# точки монтирования krb релиза и дерева исходников; tesseract ищет пары языков файлами
RUN mkdir -p "/tree${BOBA_DIR}/conf/krb" "/tree${SANDBOX_DEV_KRB}"             \
    && ln -sf eng.traineddata /tree/usr/share/tessdata/rus+eng.traineddata     \
    && ln -sf rus.traineddata /tree/usr/share/tessdata/eng+rus.traineddata     \
    && sh /tmp/mkrootfs.sh /tree /out/sandbox-rootfs.ext4 ${PYTHON_VERSION}


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
COPY --from=fuse2fs-build /out/sandbox-workspace.ext4                         ${BOBA_DIR}/sandbox/workspace.ext4
COPY --from=sandbox-image /out/sandbox-rootfs.ext4                            ${BOBA_DIR}/sandbox/rootfs.ext4
COPY --from=app-site /opt/app-site                                            ${BOBA_DIR}/app/
COPY --from=app-site /opt/app-packages.txt                                    ${BOBA_DIR}/app/packages.txt
# веса эмбеддера едут в песочницу биндом профиля kb: образ корня от них не растёт
COPY build/chainlit/src/fastembed/                                                     ${BOBA_DIR}/models/fastembed/

RUN mkdir -p ${BOBA_DIR}/conf ${BOBA_DIR}/app_root/public

# шебанги стадии сборки ведут на /usr/local/bin/python3, которого здесь нет
RUN sed -i "1s|^#!.*python3.*|#!${BOBA_DIR}/third/bin/python3|" ${BOBA_DIR}/app/bin/*

# TrustedBinaries не примет third/bin, доступный на запись посторонним
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
COPY build/chainlit/src/webassets/                                         ${BOBA_DIR}/app_root/public/vendor/
COPY build/chainlit/src/onnx-genai/                                        ${BOBA_DIR}/models/onnx-genai/

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
