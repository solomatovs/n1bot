# builder: gcc 8 + python 3.11 поверх готовой базы astra-linux:2.12-glibc2.28
FROM astra-linux:2.12-glibc2.28 AS builder

ARG GCC_VERSION=8.5.0
ARG GCC_PATH=/opt/gcc-8
ARG PYTHON_VERSION=3.11.12
ARG PYTHON_PATH=/opt/python3.11

# build-toolchain (bootstrap-gcc из репо) + dev-зависимости для gcc/python
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential gawk bison xz-utils gettext \
        zlib1g-dev libncurses5-dev libgdbm-dev libnss3-dev \
        libssl-dev libreadline-dev libffi-dev libsqlite3-dev \
        libbz2-dev liblzma-dev curl \
    && rm -rf /var/lib/apt/lists/*

# libc6-dev (от build-essential) перезаписал заголовки glibc на 2.24 —
# возвращаем 2.28 из базы и перелинковываем arch-заголовки multiarch на них,
# иначе сборка против 2.24-заголовков + 2.28-libc даёт implicit-declaration
COPY --from=astra-linux:2.12-glibc2.28 /usr/include/ /usr/include/
RUN set -eux \
    && cd /usr/include/x86_64-linux-gnu \
    && dpkg -L libc6-dev | grep -E '^/usr/include/x86_64-linux-gnu/[^/]+$' | while read -r p; do \
        n=${p#/usr/include/x86_64-linux-gnu/}; \
        rm -rf "$n"; ln -s "../$n" "$n"; \
    done

# GCC 8 (libstdc++)
COPY build/artifacts/gcc-src/ /tmp/gcc-src/
RUN cd /tmp \
    && tar -xf gcc-src/gcc-${GCC_VERSION}.tar.xz \
    && cd gcc-${GCC_VERSION} \
    && tar -xf /tmp/gcc-src/gmp-6.1.2.tar.xz && mv gmp-6.1.2 gmp \
    && tar -xf /tmp/gcc-src/mpfr-4.0.2.tar.xz && mv mpfr-4.0.2 mpfr \
    && tar -xf /tmp/gcc-src/mpc-1.1.0.tar.gz && mv mpc-1.1.0 mpc \
    && cd /tmp && mkdir gcc-build && cd gcc-build && \
    ../gcc-${GCC_VERSION}/configure \
        --enable-languages=c,c++ \
        --disable-multilib --disable-bootstrap \
        --disable-libsanitizer --disable-libgomp \
        --disable-libquadmath --disable-libssp \
        --disable-libvtv --disable-libada \
        --with-default-libstdcxx-abi=new \
    && make -j"$(nproc)" \
    && make install-strip DESTDIR=${GCC_PATH} \
    && cd / \
    && rm -rf /tmp/*

# Python 3.11
COPY build/artifacts/python-src/Python-${PYTHON_VERSION}.tar.xz /tmp/Python.tar.xz
RUN cd /tmp \
    && tar -xf Python.tar.xz \
    && cd Python-${PYTHON_VERSION} && \
    ./configure \
        --enable-optimizations --with-lto \
        --enable-shared \
        --with-ensurepip=install \
        LDFLAGS=-Wl,-rpath,/usr/local/lib \
    && make -j"$(nproc)" \
    && make install DESTDIR=${PYTHON_PATH} \
    && rm -rf /tmp/*


# Runtime
FROM astra-linux:2.12-glibc2.28 AS base

ARG GCC_PATH=/opt/gcc-8
ARG PYTHON_PATH=/opt/python3.11

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0 \
        liblzma5 libreadline7 libncurses5 zlib1g curl \
        binutils make \
        libkrb5-dev krb5-user \
        imagemagick \
        libreoffice-writer libreoffice-calc libreoffice-impress \
        ghostscript \
        fonts-dejavu-core \
        bubblewrap \
        git \
        dnsutils \
        vim \
        telnet \
    && rm -rf /var/lib/apt/lists/*

# PDF-coder ImageMagick
RUN for p in /etc/ImageMagick-6/policy.xml /etc/ImageMagick-7/policy.xml; do \
        if [ -f "$p" ]; then \
            sed -i 's#rights="none" pattern="PDF"#rights="read|write" pattern="PDF"#' "$p"; \
        fi; \
    done

# liteparse ждёт унифицированный CLI magick
RUN printf '%s\n' \
        '#!/bin/sh' \
        'case "$1" in' \
        '  convert|identify|mogrify|composite|montage|compare|stream|conjure|import) c="$1"; shift; exec "$c" "$@";;' \
        '  *) exec convert "$@";;' \
        'esac' \
        > /usr/local/bin/magick \
    && chmod +x /usr/local/bin/magick

# для разных программ свои переменные окружения CA bundle
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt

# российская локаль ru_RU.UTF-8
RUN set -eux; \
    mkdir -p /usr/lib/x86_64-linux-gnu/locale; \
    localedef -i ru_RU -c -f UTF-8 ru_RU.UTF-8; \
    locale -a | grep -q '^ru_RU\.utf8$'
ENV LANG=ru_RU.UTF-8 \
    LC_ALL=ru_RU.UTF-8 \
    LANGUAGE=ru_RU.UTF-8

# gcc 8 и python 3.11
COPY --from=builder ${GCC_PATH} /
COPY --from=builder ${PYTHON_PATH} /

RUN set -eux; \
    printf '%s\n' /usr/local/lib64 /usr/local/lib > /etc/ld.so.conf.d/000-gcc8.conf; \
    ldconfig; \
    ldconfig -p | grep -q '/usr/local/lib64/libstdc++.so.6' && echo "libstdc++ gcc8: OK"; \
    ldd --version | head -n1; \
    gcc --version | head -n1; \
    python3 --version; \
    pip3 --version

# pip.conf
COPY build/artifacts/pip.conf* /tmp/pipconf/
RUN if [ -f /tmp/pipconf/pip.conf ]; then cp /tmp/pipconf/pip.conf /etc/pip.conf; fi && rm -rf /tmp/pipconf

# uv.toml
COPY build/artifacts/uv.toml* /tmp/uvconf/
RUN if [ -f /tmp/uvconf/uv.toml ]; then mkdir -p /etc/uv && cp /tmp/uvconf/uv.toml /etc/uv/uv.toml; fi && rm -rf /tmp/uvconf

# uv — offline-пакетный менеджер рядом с pip (статич. бинарь; есть в boba-base и runtime-образе)
COPY build/artifacts/bin/uv /usr/local/bin/uv

FROM base AS deps

COPY build/artifacts/wheels/ /tmp/wheels/

# copy from sources
COPY packages/ /tmp/boba/packages/

# deps offline install
# подпроекты вычисляются автоматически: каждый pyproject.toml внутри packages/ — это пакет
# (сама директория packages/ исключается тем, что ищем со -mindepth 1)
RUN set -eu; \
    pkgs=$(find /tmp/boba/packages -mindepth 1 -name pyproject.toml -printf '%h\n' | sort); \
    names=$(for p in $pkgs; do basename "$p"; done); \
    uv pip install --system --no-index --find-links=/tmp/wheels $pkgs && \
    uv pip uninstall --system $names


FROM base

COPY build/artifacts/wheels/ /tmp/wheels/

COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin                          /usr/local/bin

# исходники всех пакетов; подпроекты вычисляются автоматически по pyproject.toml.
# --no-deps: внешние зависимости уже стоят из stage deps, ставим только сами пакеты
COPY packages/ /app/packages/
RUN set -eu; \
    for d in $(find /app/packages -mindepth 1 -name pyproject.toml -printf '%h\n' | sort); do \
      uv pip install --system --no-deps --no-index --find-links=/tmp/wheels "$d"; \
    done

# OCR-модели
COPY build/artifacts/tessdata/ /opt/tessdata/
ENV TESSDATA_PREFIX=/opt/tessdata \
    TESSDATA_PATH=/opt/tessdata

# liteparse 2.0 hotfix, rus+eng link
RUN ln -sf eng.traineddata /opt/tessdata/rus+eng.traineddata && \
    ln -sf rus.traineddata /opt/tessdata/eng+rus.traineddata

# embedding-веса fastembed
COPY build/artifacts/fastembed/ /opt/fastembed/

WORKDIR /app
EXPOSE 8501

CMD ["boba-chainlit"]
