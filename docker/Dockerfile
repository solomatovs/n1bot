# ============================================================
# Stage 1: build glibc 2.28 + libstdc++ (GCC 8) + Python 3.11
#
# Тяжёлый шаг (~30 мин). Пересобирается ТОЛЬКО при изменении
# исходников в docker/glibc-src, docker/gcc-src, docker/python-src.
# ============================================================
FROM astra_linux_ce_2.12-rootfs:latest AS builder

ARG GLIBC_VERSION=2.28
ARG GCC_VERSION=8.5.0
ARG PYTHON_VERSION=3.11.12

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential gawk bison xz-utils \
        zlib1g-dev libncurses5-dev libgdbm-dev libnss3-dev \
        libssl-dev libreadline-dev libffi-dev libsqlite3-dev \
        libbz2-dev liblzma-dev curl \
    && rm -rf /var/lib/apt/lists/*

# ---------- glibc 2.28 ----------
COPY docker/glibc-src/glibc-${GLIBC_VERSION}.tar.xz /tmp/glibc.tar.xz
RUN cd /tmp && \
    tar -xf glibc.tar.xz && \
    mkdir glibc-build && cd glibc-build && \
    ../glibc-${GLIBC_VERSION}/configure \
        --prefix=/opt/glibc-2.28 \
        --disable-werror \
        --enable-kernel=4.15 && \
    make -j"$(nproc)" && make install && \
    rm -rf /tmp/glibc*

RUN mkdir -p /opt/glibc-2.28/etc && \
    printf '/lib/x86_64-linux-gnu\n/usr/lib/x86_64-linux-gnu\n/usr/lib\n/lib\n' \
        > /opt/glibc-2.28/etc/ld.so.conf && \
    /opt/glibc-2.28/sbin/ldconfig

RUN ln -sf /opt/glibc-2.28/lib/ld-linux-x86-64.so.2 /lib64/ld-linux-x86-64.so.2

# ---------- GCC 8 (libstdc++) ----------
COPY docker/gcc-src/ /tmp/gcc-src/
RUN cd /tmp && \
    tar -xf gcc-src/gcc-${GCC_VERSION}.tar.xz && \
    cd gcc-${GCC_VERSION} && \
    tar -xf /tmp/gcc-src/gmp-6.1.2.tar.xz && mv gmp-6.1.2 gmp && \
    tar -xf /tmp/gcc-src/mpfr-4.0.2.tar.xz && mv mpfr-4.0.2 mpfr && \
    tar -xf /tmp/gcc-src/mpc-1.1.0.tar.gz && mv mpc-1.1.0 mpc && \
    cd /tmp && mkdir gcc-build && cd gcc-build && \
    ../gcc-${GCC_VERSION}/configure \
        --prefix=/opt/gcc-8 \
        --enable-languages=c,c++ \
        --disable-multilib --disable-bootstrap \
        --disable-libsanitizer --disable-libgomp \
        --disable-libquadmath --disable-libssp \
        --disable-libvtv --disable-libada \
        --with-default-libstdcxx-abi=new && \
    make -j"$(nproc)" && make install-strip && \
    rm -rf /tmp/gcc* /tmp/gcc-src

# ---------- Python 3.11 ----------
COPY docker/python-src/Python-${PYTHON_VERSION}.tar.xz /tmp/Python.tar.xz
RUN cd /tmp && \
    tar -xf Python.tar.xz && \
    cd Python-${PYTHON_VERSION} && \
    ./configure \
        --prefix=/opt/python3.11 \
        --enable-optimizations --with-lto \
        --with-ensurepip=install && \
    make -j"$(nproc)" && make install && \
    rm -rf /tmp/Python*

# ============================================================
# Stage 2: base — чистый runtime с glibc 2.28 + gcc 8 + python 3.11
#
# Этот образ можно собрать отдельно и тегнуть:
#   docker build --target=base -t n1bot-base .
# ============================================================
FROM astra_linux_ce_2.12-rootfs:latest AS base

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0 \
        liblzma5 libreadline7 libncurses5 zlib1g curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/glibc-2.28 /opt/glibc-2.28
RUN ln -sf /opt/glibc-2.28/lib/ld-linux-x86-64.so.2 /lib64/ld-linux-x86-64.so.2 && \
    ln -sf /opt/glibc-2.28/lib/libc.so.6 /lib/x86_64-linux-gnu/libc.so.6

COPY --from=builder /opt/gcc-8/lib64/libstdc++.so.6* /usr/lib/x86_64-linux-gnu/
COPY --from=builder /opt/gcc-8/lib64/libgcc_s.so.1  /lib/x86_64-linux-gnu/libgcc_s.so.1
RUN /opt/glibc-2.28/sbin/ldconfig

COPY --from=builder /opt/python3.11 /opt/python3.11
ENV PATH="/opt/python3.11/bin:${PATH}"

RUN python3 --version && pip3 --version

# ============================================================
# Stage 3: deps — установка Python-зависимостей
# ============================================================
FROM base AS deps

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential libssl-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /tmp/pyproject.toml

# Генерируем requirements.txt из pyproject.toml
RUN pip3 install --no-cache-dir tomli && \
    python3 -c "import tomli,pathlib; data=tomli.loads(pathlib.Path('/tmp/pyproject.toml').read_text()); deps=data['tool']['poetry']['dependencies']; [print(n if v=='*' else f'{n}{v}') for n,v in deps.items() if n!='python']" > /tmp/requirements.txt

# Обновляем pip (24.0 не берёт wheels для некоторых пакетов) и устанавливаем
RUN pip3 install --no-cache-dir --upgrade pip && \
    pip3 install --no-cache-dir --only-binary=:all: -r /tmp/requirements.txt && \
    rm -rf /tmp/requirements.txt

# ============================================================
# Stage 4: runtime
# ============================================================
FROM base

WORKDIR /app

# Копируем установленные пакеты из deps stage
COPY --from=deps /opt/python3.11/lib/python3.11/site-packages /opt/python3.11/lib/python3.11/site-packages
COPY --from=deps /opt/python3.11/bin /opt/python3.11/bin

COPY src/ ./src/

EXPOSE 8501

HEALTHCHECK CMD curl --fail http://localhost:8501/_stcore/health || exit 1

ENTRYPOINT ["streamlit", "run", "src/app.py", \
    "--server.port=8501", \
    "--server.address=0.0.0.0", \
    "--server.headless=true"]
