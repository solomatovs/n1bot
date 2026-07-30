# boba-chainlit — портативный релиз __RELEASE_NAME__

Собран из образа `__IMAGE__`, но операционной системы внутри нет — только
артефакты сборки: python с установленными пакетами boba, веса fastembed, модели
OCR и библиотеки, которых может не оказаться на сервере. Docker и системный
python не нужны.

```
__RELEASE_NAME__/
  python/                 python и все пакеты (bin/, lib/python3.11/site-packages)
  lib/                    .so, доставленные из образа (libstdc++, ssl, kerberos, …)
  opt/tessdata/           модели OCR
  opt/fastembed/          веса эмбеддингов
  conf/                   настройки — правятся руками
    config.toml           конфиг приложения (заполнить!)
    prompts/              системные промпты
  local/                  данные — пишет само приложение
    workspaces/           рабочие каталоги агента
    .chainlit/            runtime-состояние chainlit
  boba.env                переменные окружения (пути релиза) — единственное место,
                          где прописаны пути; unit подключает его через EnvironmentFile
  boba-chainlit.service   unit для systemd
  IMAGE.txt               из какого образа собран
```

## Перенос на сервер

```sh
tar -C .. -czf __RELEASE_NAME__.tar.gz __RELEASE_NAME__      # на машине сборки
scp __RELEASE_NAME__.tar.gz server:/tmp/
ssh server 'mkdir -p /opt/boba && tar -C /opt/boba -xzf /tmp/__RELEASE_NAME__.tar.gz'
```

Версионирование — каталогами: `/opt/boba/<релиз>`. Релизы независимы: `conf/` и
`local/` переносятся из старого в новый как есть.

## Установка

1. Заполнить `conf/config.toml` (обязательно: `auth_secret`, `[openai.*]`,
   способы входа в `[chainlit].auth`).
2. Поставить службу:
   ```sh
   cp boba-chainlit.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now boba-chainlit
   journalctl -u boba-chainlit -f
   ```

Приложение слушает host/port из `config.toml`.

Запустить руками, тем же окружением и без systemd (например чтобы посмотреть
вывод при разборе проблемы):

```sh
set -a; . ./boba.env; set +a
python/bin/python3 -m boba.chainlit
```

## Если каталог релиза другой

Пути прописаны на `__INSTALL_DIR__` в двух файлах — `boba.env` и unit. Проще
всего собрать релиз сразу под нужный путь (`make extract INSTALL_DIR=<путь>`),
но можно и переписать на месте:

```sh
sed -i 's|__INSTALL_DIR__|/новый/путь|g' boba.env boba-chainlit.service
```

## Что может понадобиться доустановить

Релиз тянет с собой библиотеки, специфичные для сборки, но рассчитывает на
системный glibc и на внешние инструменты. Если они нужны — поставить пакетами:

| зачем | debian/ubuntu |
|---|---|
| обработка документов (`[tool.doc]`) | `imagemagick ghostscript libreoffice-writer libreoffice-calc libreoffice-impress` |
| песочница для shell-инструмента (`[tool.shell]`) | `bubblewrap` |
| kerberos-утилиты для отладки (`kinit`, `klist`) | `krb5-user` |
| корневые сертификаты | `ca-certificates` |

Само приложение, web-UI, LDAP/Kerberos-вход, работа с postgres и эмбеддинги
работают без этих пакетов.

Проверить, чего не хватает библиотекам релиза:

```sh
LD_LIBRARY_PATH=lib:python/lib ldd python/bin/python3 | grep 'not found'
```

## Kerberos

Файл `krb5.conf` и keytab берутся с сервера по системным путям
(`/etc/krb5.conf`, путь к keytab указывается в `config.toml`). Библиотеки
kerberos лежат в релизе, ставить `libkrb5` отдельно не нужно.

## Что где лежит

| что | путь |
|---|---|
| интерпретатор | `python/bin/python3` |
| пакеты boba и зависимости | `python/lib/python3.11/site-packages` |
| консольные скрипты | `python/bin/boba-chainlit`, `boba-chainlit2`, `boba-cli` |
| веса fastembed | `opt/fastembed` |
| модели OCR tesseract | `opt/tessdata` |
| конфиг и промпты | `conf/` |
| данные приложения | `local/` |
