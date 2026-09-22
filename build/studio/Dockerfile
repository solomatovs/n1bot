ARG PYTHON_VERSION=3.11
ARG GLIBC_IMAGE=dmp/glibc:2.28
ARG GCC_IMAGE=dmp/gcc:8.5.0
ARG PYTHON_IMAGE=dmp/python:${PYTHON_VERSION}-dist
ARG NODE_IMAGE=dmp/nodejs:22

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

RUN LD_LIBRARY_PATH=/python/lib PYTHONHOME=/python /python/bin/python3 -c "import ssl, sqlite3, lzma, bz2; print('python-slim ok')"


# закрытый контур: CA внутреннего TLS и индексы пакетов из build/studio/conf.
# Стадии берут готовые деревья: /certs — всем, /index — только сборочным
FROM ${GLIBC_IMAGE} AS site-conf

COPY build/studio/conf/  /tmp/conf/

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
COPY build/studio/src/uv/uv        /usr/local/bin/uv

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

COPY build/studio/src/bubblewrap-${BWRAP_VERSION}.tar.xz  /tmp/bwrap.tar.xz

WORKDIR /tmp/bwrap-src
RUN tar -xJf /tmp/bwrap.tar.xz --strip-components=1
RUN meson setup _build --buildtype=release --prefix=/usr -Dselinux=disabled -Dman=disabled \
    && sed -i 's|/usr/lib/x86_64-linux-gnu/libcap\.so|/usr/lib/x86_64-linux-gnu/libcap.a|g' _build/build.ninja \
    && ninja -C _build bwrap
RUN strip _build/bwrap && install -D -m 0755 _build/bwrap /out/bwrap

RUN ! ldd /out/bwrap | grep libcap && /out/bwrap --version


# fuse2fs статически с libfuse, плюс шаблон workspace тем же mke2fs, что уедет в релиз
FROM base AS fuse2fs-build

ARG LIBFUSE_VERSION=3.12.0
ARG E2FSPROGS_VERSION=1.47.2

RUN apt-get update && apt-get install -y --no-install-recommends \
        meson ninja-build pkg-config xz-utils                    \
    && rm -rf /var/lib/apt/lists/*

COPY build/studio/src/fuse-${LIBFUSE_VERSION}.tar.xz         /tmp/fuse.tar.xz
COPY build/studio/src/e2fsprogs-${E2FSPROGS_VERSION}.tar.xz  /tmp/e2fsprogs.tar.xz

WORKDIR /tmp/fuse-src
RUN tar -xJf /tmp/fuse.tar.xz --strip-components=1
RUN meson setup _build --buildtype=release --prefix=/usr/local \
        -Ddefault_library=static -Dexamples=false -Dtests=false -Dutils=false \
    && ninja -C _build install

WORKDIR /tmp/e2fs-src
ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig:/usr/local/lib/x86_64-linux-gnu/pkgconfig
RUN tar -xJf /tmp/e2fsprogs.tar.xz --strip-components=1
RUN LIBS="-lpthread -ldl" ./configure --enable-fuse2fs --disable-nls && make -j"$(nproc)"
RUN strip misc/fuse2fs && install -D -m 0755 misc/fuse2fs /out/fuse2fs

RUN ! ldd /out/fuse2fs | grep fuse && /out/fuse2fs -V

# mke2fs линкован только с libc: им make делает workspace.ext4 и rootfs плагинов вне сборки
RUN install -D -m 0755 misc/mke2fs /out/mke2fs && cp misc/mke2fs.conf /out/mke2fs.conf


# артефакты песочницы для make sandbox: docker build --output, без образа
FROM scratch AS sandbox-out

COPY --from=bwrap-build   /out/bwrap        /third/bin/bwrap
COPY --from=fuse2fs-build /out/fuse2fs      /third/bin/fuse2fs
COPY --from=fuse2fs-build /out/mke2fs       /tools/mke2fs
COPY --from=fuse2fs-build /out/mke2fs.conf  /tools/mke2fs.conf


# колёса всех пакетов репозитория и constraints из uv.lock: обе стадии установки
# получают версии хоста, иначе uv ставил бы свежайшие
FROM base AS boba-wheels

COPY pyproject.toml  /tmp/boba/pyproject.toml
COPY uv.lock         /tmp/boba/uv.lock
COPY packages/       /tmp/boba/packages/

WORKDIR /tmp/boba
RUN uv build --all-packages --wheel --no-sources -o /tmp/wheels
RUN uv export --frozen --all-packages --all-extras --no-emit-workspace --no-hashes \
        -o /tmp/wheels/constraints.txt


# site-packages приложения: ставится по своему графу зависимостей, парсеры и
# эмбеддер сюда не попадают — они объявлены как extra payload у tool-пакетов
FROM base AS app-site

ARG APP_PACKAGES="boba-studio"
ARG APP_IMPORTS="boba.studio"

COPY --from=boba-wheels /tmp/wheels/  /tmp/wheels/

RUN uv pip install --python "$(command -v python3)" --target /opt/app-site \
        --constraint /tmp/wheels/constraints.txt --find-links=/tmp/wheels \
        ${APP_PACKAGES}

ENV PYTHONPATH=/opt/app-site
RUN for name in ${APP_IMPORTS}; do python3 -c "import $name; print('$name ok')"; done
RUN pip3 list --path /opt/app-site --format=freeze | grep '^boba-' | cut -d= -f1 > /opt/app-packages.txt


# --- образ корня одного плагина: make plugin-rootfs PLUGIN=<пакет> ---------------

# site-packages плагина: закрытие payload одного пакета и его сборочные декларации
FROM base AS plugin-site

ARG PLUGIN

COPY --from=boba-wheels /tmp/wheels/          /tmp/wheels/
COPY --from=boba-wheels /tmp/boba/packages/   /tmp/packages/
COPY build/studio/scripts/plugin_rootfs.py  /tmp/plugin_rootfs.py

RUN test -n "${PLUGIN}"
RUN python3 /tmp/plugin_rootfs.py names /tmp/packages ${PLUGIN} > /tmp/names.txt && cat /tmp/names.txt
RUN uv pip install --python "$(command -v python3)" --target /opt/site \
        --constraint /tmp/wheels/constraints.txt --find-links=/tmp/wheels \
        $(cat /tmp/names.txt)

RUN python3 /tmp/plugin_rootfs.py apt /tmp/packages ${PLUGIN} > /opt/plugin-apt.txt
RUN python3 /tmp/plugin_rootfs.py data /tmp/packages ${PLUGIN} > /opt/plugin-data.txt

RUN mkdir -p /opt/plugin-root \
    && for root in $(python3 /tmp/plugin_rootfs.py root /tmp/packages ${PLUGIN}); do \
           cp -a "$root/." /opt/plugin-root/; \
       done

RUN mkdir -p /opt/plugin-setup.d && index=0 \
    && for setup in $(python3 /tmp/plugin_rootfs.py setup /tmp/packages ${PLUGIN}); do \
           cp "$setup" "/opt/plugin-setup.d/$index.sh"; index=$((index + 1)); \
       done

ENV PYTHONPATH=/opt/site
RUN for name in $(python3 /tmp/plugin_rootfs.py imports /tmp/packages ${PLUGIN}); do \
        python3 -c "import $name; print('$name ok')"; \
    done

RUN mkdir -p /opt/code-sandbox \
    && mv /opt/site/boba /opt/code-sandbox/boba \
    && mv /opt/site/boba_*.dist-info /opt/code-sandbox/ \
    && echo /usr/src > /opt/site/boba-src.pth

# гость зиготы стартует в образе без биндов кода: контракт проверяется здесь
RUN PYTHONPATH=/opt/code-sandbox:/opt/site python3 -c "import boba.sandbox.guest; print('guest ok')"


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
COPY --from=python-slim /python/                                               /usr/local/
RUN rm -f /usr/lib/x86_64-linux-gnu/libstdc++.so.${LIBSTDCXX_OLD} \
    && ln -sf libstdc++.so.${LIBSTDCXX_VERSION} /usr/lib/x86_64-linux-gnu/libstdc++.so.6 \
    && ldconfig

RUN rm -rf /usr/share/man /usr/share/doc /usr/share/info /usr/share/locale \
           /usr/share/lintian /var/cache/* /var/log/*

RUN mkdir -p /usr/src /workspace /mnt/pgsock


# корень одного плагина поверх базы: apt и оверлей из декларации, точки монтирования.
# site-packages сюда не копируются: make plugin-rootfs стримит эту стадию и
# plugin-site двумя tar'ами в mke2fs (docker build --output type=tar), ext4 и
# копии site в build cache не оседают
FROM sandbox-base AS plugin-root

ARG BOBA_DIR=/app/boba
ARG SANDBOX_DEV_KRB=/app/boba/conf/krb

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
RUN mkdir -p "${BOBA_DIR}/conf/krb" "${SANDBOX_DEV_KRB}"


# --- фронт: стадии node, на хосте node не нужен -----------------------------------

# контракт api: OpenAPI-документ из установленного приложения, без хоста и конфига
FROM app-site AS openapi

RUN python3 -m boba.studio.api.schema > /tmp/openapi.json


# страница workflow: типы из openapi.json стадии выше
FROM ${NODE_IMAGE} AS web-build

COPY packages/agents/boba-studio/web/workflow/  /src/web/workflow/
COPY --from=openapi /tmp/openapi.json           /src/web/workflow/openapi.json

WORKDIR /src/web/workflow
RUN npm ci --no-audit --no-fund && npm run generate && npm run check && npm run build


# --- образ приложения --------------------------------------------------------------

FROM ${GLIBC_IMAGE} AS runtime-base

ARG PYTHON_VERSION=3.11
ARG LIBSTDCXX_VERSION=6.0.25
ARG BOBA_DIR=/app/boba
ARG HEALTH_PATH=/api/openapi.json

RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0                \
        liblzma5 libreadline7 libncurses5 zlib1g                 \
        libkrb5-3 libgssapi-krb5-2 krb5-user                     \
    && rm -rf /var/lib/apt/lists/*

COPY --from=site-conf /certs/                                                 /
COPY --from=python-slim /python/                                              ${BOBA_DIR}/third/
COPY --from=base /usr/lib/x86_64-linux-gnu/libstdc++.so.6                     ${BOBA_DIR}/third/lib/
COPY --from=base /usr/lib/x86_64-linux-gnu/libstdc++.so.${LIBSTDCXX_VERSION}  ${BOBA_DIR}/third/lib/
COPY --from=bwrap-build /out/bwrap                                            ${BOBA_DIR}/third/bin/bwrap
COPY --from=fuse2fs-build /out/fuse2fs                                        ${BOBA_DIR}/third/bin/fuse2fs
COPY --from=app-site /opt/app-site                                            ${BOBA_DIR}/app/
COPY --from=app-site /opt/app-packages.txt                                    ${BOBA_DIR}/app/packages.txt
COPY build/studio/scripts/healthcheck.py                                    ${BOBA_DIR}/app/healthcheck.py

RUN mkdir -p ${BOBA_DIR}/conf ${BOBA_DIR}/app_root/public ${BOBA_DIR}/sandbox

# шебанги стадии сборки ведут на /usr/local/bin/python3, которого здесь нет
RUN sed -i "1s|^#!.*python3.*|#!${BOBA_DIR}/third/bin/python3|" ${BOBA_DIR}/app/bin/*

# TrustedBinaries не примет third/bin, доступный на запись посторонним
RUN chmod -R go-w ${BOBA_DIR}/third ${BOBA_DIR}/sandbox \
    && test -z "$(find ${BOBA_DIR}/third/bin -maxdepth 1 -type f -perm /022)" \
    && ${BOBA_DIR}/third/bin/bwrap --version \
    && ${BOBA_DIR}/third/bin/fuse2fs -V

WORKDIR /app
ENV BOBA_DIR=${BOBA_DIR} \
    BOBA_HEALTH_PATH=${HEALTH_PATH}

# порт, префикс и сам python приходят из boba.env: проба идёт тем же интерпретатором
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python3 "${BOBA_DIR}/app/healthcheck.py"

ENTRYPOINT ["python3"]


# образ studio: страница workflow и vendor-шрифты; моделей чата нет
FROM runtime-base AS runtime-studio

ARG BOBA_DIR=/app/boba
ARG BOBA_UID=1000
ARG BOBA_GID=1000

COPY --chown=${BOBA_UID}:${BOBA_GID} --from=web-build /src/assets/workflow/  ${BOBA_DIR}/app_root/public/workflow/
COPY --chown=${BOBA_UID}:${BOBA_GID} build/studio/src/webassets/             ${BOBA_DIR}/app_root/public/vendor/

# файлы уже принадлежат пользователю, каталогам владелец ставится без рекурсии
RUN chown ${BOBA_UID}:${BOBA_GID} ${BOBA_DIR}/app_root ${BOBA_DIR}/app_root/public

EXPOSE 8502
USER ${BOBA_UID}:${BOBA_GID}
CMD ["-m", "boba.studio", "--config", "/app/boba/conf/config.toml"]
