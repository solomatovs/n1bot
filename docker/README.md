# Boba Docker

Astra Linux CE 2.12 + glibc 2.28 + Python 3.11.

## Быстрый старт

```bash
cd docker
cp config/config.example.toml config/config.toml
mkdir -p secrets
echo "your-litellm-key" > secrets/litellm_api_key
echo "your-confluence-token" > secrets/confluence_token
docker compose build
docker compose up -d
```

## Пересборка base-образа (только при первой установке)

Требует исходники в `glibc-src/`, `gcc-src/`, `python-src/` (~130 МБ).

```bash
# скачать исходники
mkdir -p glibc-src gcc-src python-src
curl -L -o glibc-src/glibc-2.28.tar.xz      https://ftp.wayne.edu/gnu/glibc/glibc-2.28.tar.xz
curl -L -o gcc-src/gcc-8.5.0.tar.xz         https://ftp.wayne.edu/gnu/gcc/gcc-8.5.0/gcc-8.5.0.tar.xz
curl -L -o gcc-src/gmp-6.1.2.tar.xz         https://ftp.wayne.edu/gnu/gmp/gmp-6.1.2.tar.xz
curl -L -o gcc-src/mpfr-4.0.2.tar.xz        https://ftp.wayne.edu/gnu/mpfr/mpfr-4.0.2.tar.xz
curl -L -o gcc-src/mpc-1.1.0.tar.gz         https://ftp.wayne.edu/gnu/mpc/mpc-1.1.0.tar.gz
curl -L -o python-src/Python-3.11.12.tar.xz https://www.python.org/ftp/python/3.11.12/Python-3.11.12.tar.xz

# собрать и затегировать (~30 мин)
docker build -f Dockerfile.base -t boba-base ..
```

После этого `docker compose build` использует `boba-base:latest`.

## Структура

```
docker/
├── config/
│   ├── config.toml            # [app] — всё приложение
│   ├── config.debug.toml      # то же для launch.json
│   └── config.example.toml    # шаблон (в git)
├── secrets/                   # Docker secrets (не в git)
├── import/                    # данные — папки с документами
├── Dockerfile                 # runtime (на основе boba-base)
├── Dockerfile.base            # полная сборка из astra_linux_ce
└── docker-compose.yml
```

## Команды

```bash
cd docker
docker compose build          # собрать образы
docker compose up -d          # запустить
docker compose logs -f        # логи
docker compose down           # остановить
```

## Обновление зависимостей

Зависимости ставятся из `poetry.lock` при `docker compose build` — отдельной
подготовки wheel'ов не требуется (нужен сетевой доступ к PyPI во время
сборки). После правки любого `pyproject.toml` нужно пересинхронизировать
lock — и обычный `docker compose build` подхватит новые версии.

Локальный poetry на хосте может быть сломан, поэтому lock удобно гонять
внутри `boba-base` (Python/glibc совпадают с рантаймом):

```bash
# из корня репозитория (cd n1bot)
docker run --rm --entrypoint sh --network=host -v "$PWD":/src -w /src boba-base:latest -c '
  set -e
  ln -sf /opt/python3.11/bin/python3 /usr/local/bin/python
  python3 -m venv /tmp/poetry
  /tmp/poetry/bin/pip install -q poetry
  /tmp/poetry/bin/poetry lock --no-interaction
' && rm -rf .venv
```

Обновлённый `poetry.lock` коммить вместе с `pyproject.toml`.

## Сервисы

- `chainlit` — Web UI. Наружу не выставляется: доступен внутри сети
  `docker` по hostname `boba-chainlit:8080` и публично через nginx
  reverse-proxy на `https://loshara.com/boba/`.
  Параметры (host/port/root_path/auth_secret) читаются из секции
  `[chainlit]` `config/config.toml` — CLI-аргументы не используются.
  `LITELLM_API_KEY` и `CHAINLIT_AUTH_SECRET` — через Docker secrets
  (`secrets/litellm_api_key`, `secrets/chainlit_auth_secret`).
  Workspaces и логи — в именованных volume'ах `chainlit-workspaces` /
  `chainlit-logs`.
