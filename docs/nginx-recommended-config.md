# Nginx перед boba: рекомендуемая конфигурация

Boba состоит из двух HTTP-приложений: chainlit (чат, порт 8501) и studio
(API и страницы workflow, порт 8502). Оба развёрнуты на своих префиксах
(`/boba/`, `/boba-studio/`) и ожидают путь целиком, без срезания префикса.
Nginx перед ними должен уметь три вещи: пропускать веб-сокеты, не
буферизовать потоковые ответы и принимать заголовки крупнее дефолтных
восьми килобайт.

## Конфигурация

```nginx
server {
  listen 443 ssl;
  server_name boba.example.com;

  large_client_header_buffers 4 32k;

  location /boba/ {
    resolver 127.0.0.11 valid=5s;
    set $chainlit http://chainlit:8501;
    proxy_pass $chainlit;

    include /etc/nginx/conf.d/options/boba-headers.conf;
  }

  location /boba-studio/ {
    resolver 127.0.0.11 valid=5s;
    set $studio http://studio:8502;
    proxy_pass $studio;

    include /etc/nginx/conf.d/options/boba-headers.conf;
  }
}
```

`conf.d/options/boba-headers.conf`:

```nginx
proxy_http_version 1.1;
proxy_cache_bypass $http_upgrade;

proxy_set_header Upgrade           $http_upgrade;
proxy_set_header Connection        "upgrade";
proxy_set_header Host              $http_host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host  $http_host;
proxy_set_header X-Forwarded-Port  $server_port;

proxy_buffering         off;
proxy_request_buffering off;

proxy_buffer_size       32k;
proxy_buffers           8 32k;
proxy_busy_buffers_size 64k;

proxy_read_timeout 86400s;
```

## Параметры

- **`proxy_http_version 1.1` и заголовки `Upgrade`/`Connection`.** По умолчанию
  nginx ходит к бэкенду по HTTP/1.0, где нет механизма Upgrade. Без этих
  строк веб-сокет чата не поднимется: приложение ответит ошибкой рукопожатия.
- **`proxy_cache_bypass $http_upgrade`.** Запрос на смену протокола не должен
  отдаваться из кэша.
- **`Host`, `X-Forwarded-Host`, `X-Forwarded-Proto`, `X-Forwarded-Port`.**
  Приложение строит абсолютные ссылки и редиректы по этим заголовкам. Без
  `X-Forwarded-Proto` редиректы уйдут на `http://` и войдут в цикл. `$http_host`
  вместо `$host`: первый передаёт заголовок как есть, второй отбрасывает порт.
- **`X-Real-IP`, `X-Forwarded-For`.** Адрес клиента для логов и ограничений.
- **`proxy_buffering off`, `proxy_request_buffering off`.** Чат отдаёт ответ
  модели потоком, и каждая порция должна доезжать сразу. С буферизацией
  ответ приходит рывками или застревает до конца хода. Выключенная
  буферизация запроса нужна, чтобы загрузка файла шла в приложение сразу.
- **`proxy_buffer_size 32k`, `proxy_buffers 8 32k`, `proxy_busy_buffers_size 64k`.**
  JWT входа несёт билет доменной аутентификации и едет тремя cookie общим
  объёмом около десяти килобайт. Дефолтные буферы рассчитаны на восемь
  килобайт заголовков ответа, и вход через SSO падает с 400 или 431.
- **`large_client_header_buffers 4 32k`.** То же самое для запроса от браузера.
  Стоит на уровне `server`. Нужны обе директивы: поднять одну и оставить
  другую даёт ту же ошибку с другой стороны.
- **`proxy_read_timeout 86400s`.** Веб-сокет чата и длинные ходы модели не
  должны обрываться по таймауту простоя.
- **`resolver` и `proxy_pass` через переменную.** Nginx резолвит имя из
  `proxy_pass` один раз при старте. После пересоздания контейнера он ходит
  на старый адрес и отдаёт 502 до перезагрузки. С переменной имя резолвится
  на каждый запрос через DNS docker с кэшем в пять секунд. Побочный эффект:
  с переменной nginx не срезает префикс пути, и приложение получает путь
  целиком. Для boba это и нужно.

## Диагностика

| Симптом | Причина | Что смотреть |
|---|---|---|
| 502 после пересоздания контейнера | адрес закэширован | пара `resolver`/`set` в `location` |
| Веб-сокет не поднимается | нет `Upgrade`/`Connection` или два заголовка `Connection` в блоке | `proxy_set_header` в этом `location` |
| Ответ модели приходит рывками | включена буферизация или gzip для пути | `proxy_buffering`, `gzip_types` |
| 400 или 431 после входа через SSO | заголовки не помещаются в буферы | `large_client_header_buffers`, `proxy_buffer_size` |
| Редиректы уходят на `http://` | нет `X-Forwarded-Proto` | заголовки в `boba-headers.conf` |
| Приложение видит всех клиентов как один адрес | `X-Real-IP` не передан или перед nginx ещё один прокси | `X-Real-IP`, `set_real_ip_from` |

## Применение правок

```
docker exec nginx nginx -t
docker exec nginx nginx -s reload
```

Перезагрузка не рвёт открытые соединения: старые воркеры дорабатывают
текущие запросы и завершаются сами.
