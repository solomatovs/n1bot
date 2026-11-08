# ACP V2: использование SDK и авторизация boba-cli

Дата: 2026-11-08. Статус: задание на реализацию, не описание готовых возможностей.
Область: boba serve, общий ACP-адаптер, затронутый клиент boba-aid, boba-auth и
boba-oauth. Для boba --stdio меняется протокольная обвязка; существующий локальный
режим без отдельного входа сохраняется.

Действующий контракт авторизации находится здесь;
[справочник ACP V2](acp-v2-model-configuration.md) ссылается на него из раздела 10.
Новые настройки ниже — решения Boba, а не имена, установленные стандартом ACP.
Доступ к самой службе проверяется до выполнения ACP-запросов. Отдельного
ACP-входа в этом задании нет: credentials модельного провайдера уже настроены.

## 1. Результат работы

1. Использовать публичные методы SDK для расширений и URL-elicitation.
2. Убрать ненужную поддержку пакетного JSON-RPC по stdio, сохранив корректные
   ответы на ошибки и необходимые обходы несовместимости SDK.
3. Реализовать человеческий и машинный вход, работающие через одинаковую систему
   идентификации, назначения ролей и доступа к истории.
4. Сохранить local, LDAP, Kerberos и proxy через существующие компоненты boba-auth.
5. Поддержать фиксированный Bearer и OAuth access tokens для машин. Собственный
   OAuth и корпоративные issuer должны подключаться конфигурацией, в том числе
   одновременно; собственный issuer не должен быть обязательным.
6. Возвращать authMethods: [] в serve и stdio. После проверки HTTP credentials
   и initialize сразу разрешать операции сессий в пределах прав пользователя;
   auth/login и auth/logout не объявлять, второе состояние входа не создавать.

Основа — agent-client-protocol 1.0.0rc3 и acp.experimental.v2. ACP V2 пока Draft,
HTTP/WS и MCP-over-ACP — Active RFD. Зафиксировать используемый срез спецификации
в проверках: исходный срез справочника —
[7ec5185c18ca928b48e457d2d8a163800be013c8](https://github.com/agentclientprotocol/agent-client-protocol/tree/7ec5185c18ca928b48e457d2d8a163800be013c8).
Если нужное требование отсутствует в этом срезе, явно обновить pin и тестовые
схемы до выбранной ревизии, а не смешивать определения незаметно.

## 2. Короткая карта кода

Пути Boba указаны от корня репозитория. Локальный корень SDK:
.venv/lib/python3.11/site-packages/acp/. Ссылки SDK закреплены на 1.0.0rc3;
изменять site-packages вручную нельзя.

### Официальный SDK

- [Connection — acp/connection.py](https://github.com/agentclientprotocol/python-sdk/blob/1.0.0rc3/src/acp/connection.py):
  JSON-RPC, ожидание ответов, входящие задачи, отмена запросов, observers.
- [AgentSideConnection — acp/experimental/v2/agent.py](https://github.com/agentclientprotocol/python-sdk/blob/1.0.0rc3/src/acp/experimental/v2/agent.py):
  session_update, request_permission, create_elicitation, complete_elicitation,
  send_extension_request/notification, attach.
- [ClientSideConnection — acp/experimental/v2/client.py](https://github.com/agentclientprotocol/python-sdk/blob/1.0.0rc3/src/acp/experimental/v2/client.py):
  initialize, login/logout, операции сессий, отправка расширений.
- [Agent, Client — acp/experimental/v2/interfaces.py](https://github.com/agentclientprotocol/python-sdk/blob/1.0.0rc3/src/acp/experimental/v2/interfaces.py)
  и [schema.py](https://github.com/agentclientprotocol/python-sdk/blob/1.0.0rc3/src/acp/experimental/v2/schema.py):
  интерфейсы обработчиков и модели V2; готового OAuth-входа здесь нет.
- [AcpServer — acp/http/server.py](https://github.com/agentclientprotocol/python-sdk/blob/1.0.0rc3/src/acp/http/server.py),
  [create_asgi_app — http/asgi.py](https://github.com/agentclientprotocol/python-sdk/blob/1.0.0rc3/src/acp/http/asgi.py),
  [handle_websocket — ws/server.py](https://github.com/agentclientprotocol/python-sdk/blob/1.0.0rc3/src/acp/ws/server.py):
  connection IDs, HTTP/SSE, очереди и WebSocket.
- [AgentProtocolRouter — experimental/negotiation.py](https://github.com/agentclientprotocol/python-sdk/blob/1.0.0rc3/src/acp/experimental/negotiation.py),
  [InitializationState — v2/_initialization.py](https://github.com/agentclientprotocol/python-sdk/blob/1.0.0rc3/src/acp/experimental/v2/_initialization.py),
  [MethodRouter — v2/_router.py](https://github.com/agentclientprotocol/python-sdk/blob/1.0.0rc3/src/acp/experimental/v2/_router.py):
  выбор версии, состояние initialize, доставка методов и расширений.
- [Transport, NdjsonTransport — acp/_transport.py](https://github.com/agentclientprotocol/python-sdk/blob/1.0.0rc3/src/acp/_transport.py):
  граница транспорта и построчный JSON. Модуль внутренний; использование
  сосредоточить в одном адаптере с тестами совместимости.

### Boba: ACP и запуск

- [protocol.py](../packages/infra/transport/boba-transport-acp/src/boba/transport/acp/protocol.py):
  AgentSide — проверки перед роутером SDK и MCP-over-ACP; ClientConnection —
  адаптер клиентской стороны; BatchTransport — текущий stdio с пакетами;
  BobaExtension — имена собственных расширений.
- [serve.py](../packages/apps/boba-cli/src/boba/cli/serve.py):
  ServeHost — ASGI/OAuth-сборка и authMethods; CallerTokens — текущая привязка
  к собственному JWT; AcpEndpoint — проверка владельца и активности соединения;
  ServeRouter/ServeConnection — создание и закрытие; ServeAgents — профили
  и хранилище истории пользователя.
- [acp_server.py](../packages/apps/boba-cli/src/boba/cli/acp_server.py):
  AcpAgent — обработчики ACP, сейчас login фактически пустой;
  ClientLink — вызовы SDK из потоков сессий; SessionRelay — события ядра,
  разрешения и формы; AcpToolServer/SessionMcp — инструменты MCP.
- [app.py](../packages/apps/boba-cli/src/boba/cli/app.py):
  AgentProcess/StdioStreams — локальный запуск и отдельный stdout для протокола.
  [config/models.py](../packages/apps/boba-cli/src/boba/cli/config/models.py):
  ServeConfig/ServeSection — конфигурация службы.
- [boba-aid/agent/client.py](../packages/apps/boba-aid/src/boba/aid/agent/client.py):
  AgentClient — обработчики клиента; AgentService — подключение к службе;
  AgentProcess — запуск stdio. При пустом authMethods не вызывать login/logout;
  сохранить получение Bearer штатным HTTP auth-провайдером.

### Общая авторизация

- [StandardAuthBackends](../packages/services/boba-auth/src/boba/auth/providers/backends.py),
  [AuthBackends/AuthContribution](../packages/services/boba-auth/src/boba/auth/assembly.py):
  сборка существующих бэкендов. [SignIns](../packages/services/boba-auth/src/boba/auth/signin.py):
  by_password, by_proxy, admit_proxy, exchange; никаких проверок пароля в ACP.
- [LocalSignIn/LdapSignIn/CompositeSignIn](../packages/services/boba-auth/src/boba/auth/providers/password.py):
  парольные проверки; LDAP через
  [Ldap3Directory](../packages/infra/auth/boba-ldap/src/boba/ldap/directory.py).
  [SpnegoGate](../packages/services/boba-auth/src/boba/auth/providers/kerberos.py)
  использует [SpnegoAcceptor/TicketCapture](../packages/infra/auth/boba-krb/src/boba/krb/delegation.py).
  [HmacProxySignIn](../packages/services/boba-auth/src/boba/auth/providers/proxy.py)
  проверяет существующий proxy-вход. Эталон приложения —
  [AID ProxyAuth](../packages/apps/boba-aid/src/boba/aid/auth/proxy.py) и
  [BrowserSessions.by_proxy](../packages/apps/boba-aid/src/boba/aid/auth/session.py):
  [ProxyRequests](../packages/services/boba-auth/src/boba/auth/transport/http.py)
  читает заголовки, затем вызывается общий SignIns.by_proxy.
- [SessionService](../packages/services/boba-auth/src/boba/auth/service.py):
  accept/current, сохранение личности и актуализация прав;
  [SignInResultFactory](../packages/services/boba-auth/src/boba/auth/providers/result.py):
  общие роли и профили;
  [AuthenticatedPrincipal/AuthenticatedSession](../packages/core/boba-identity/src/boba/identity/authentication.py):
  подтверждённые результаты. [UserIdentity/IdentityRule](../packages/core/boba-identity/src/boba/identity/principal.py):
  каноническая личность и явные aliases.
- [AuthServer](../packages/services/boba-oauth/src/boba/oauth/server/provider.py),
  [LoginPages](../packages/services/boba-oauth/src/boba/oauth/server/pages.py),
  [ServiceTokens](../packages/services/boba-oauth/src/boba/oauth/server/tokens.py):
  собственный OAuth, браузерные local/LDAP/SPNEGO, выпуск и проверка JWT.
- [ServiceAuth/ResourceTokens](../packages/services/boba-oauth/src/boba/oauth/server/resources.py):
  защита ресурсов; [RegisteredClients/TrustedClient](../packages/services/boba-oauth/src/boba/oauth/server/clients.py):
  регистрация OAuth и отдельное доверие proxy;
  [ProxyLogin](../packages/services/boba-oauth/src/boba/oauth/server/proxy.py) и
  [ProxyAssertionVerifier](../packages/services/boba-oauth/src/boba/oauth/server/assertions.py):
  обмен проверенного proxy-подтверждения на access token.

## 3. Решение по авторизации

### 3.1. Доступ к службе до ACP

Основной удалённый сценарий: HTTP 401 → получение/предъявление Bearer → initialize
→ операции сессий. Если действительный токен уже есть, клиент сразу отправляет
его с первым запросом. Человек, программа и доверенный proxy приходят к одному
подтверждённому контексту личности и прав; дополнительный ACP-допуск не нужен.

В текущем продукте endpoint и credentials модельного провайдера задаются заранее.
ACP auth/login для подключения аккаунта провайдера сейчас не используется.
ServeHost._auth_methods возвращает пустой список. Не вводить новые methodId,
состояния login_pending/authenticated/logged_out или токен приложения с правом
только запускать интерактивный вход.

Это разрешённый вариант V2: при пустом/отсутствующем authMethods клиент не вызывает
login/logout. При будущем появлении отдельного входа агента его понадобится
объявить и реализовать весь жизненный цикл, но это не часть данного задания.
[Authentication](https://agentclientprotocol.com/protocol/v2/authentication).

### 3.2. Поддерживаемые сценарии

| Сценарий | Как устанавливается личность до ACP | Чья история |
| --- | --- | --- |
| Человек: local / LDAP / Kerberos | Вход через собственный OAuth Boba, проверки существующим boba-auth, затем пользовательский Bearer | Пользователя |
| Человек: корпоративный OAuth | Access token разрешённого корпоративного issuer | Пользователя по проверенному identity mapping |
| Машина от своего имени | Фиксированный Bearer или OAuth client_credentials | Служебной учётной записи |
| Доверенный proxy от имени пользователя | Подписанные заголовки с именем и ролями, проверенные общим boba-auth; HTTP-адаптер выдаёт Bearer | Подтверждённого пользователя, а не самого proxy |
| Дополнительный прямой local/LDAP либо Kerberos | Явно включённый HTTP Basic / Negotiate через существующие backend-проверки | Подтверждённой учётной записи |
| Локальный stdio | Существующий локальный контекст запуска | По политике локального режима |

Во всех строках authMethods пуст. Браузер нужен для выбранного человеческого
OAuth-входа, а не для работы каждой ACP-программы. Статический Bearer и
client_credentials не требуют браузера или доверенного proxy. Прямые Basic и
Negotiate — дополнительные HTTP-профили, не ACP-методы входа.

### 3.3. Подтверждённая личность и соединение

Результат проверки содержит стабильный внутренний ID пользователя/службы,
каноническую личность, актуальные роли/профили, источник и срок допуска.
Proxy-клиента учитывать как источник делегированного утверждения; владельцем
истории становится подтверждённый пользователь. Эти сведения не берутся из
initialize.info, methodId или непроверенных заголовков.

Acp-Connection-Id выбирает серверный контекст и очереди сообщений. При каждом
HTTP-запросе личность из проверенного credential сверяется с владельцем контекста.
Сам ID не даёт доступа и не обозначает один TCP-сокет. Разрыв SSE не меняет
личность: пока контекст сохранён, поток можно открыть снова с Bearer и тем же ID.
После удаления контекста выполняются initialize и session/resume с действительным
credential; повторного ACP-входа нет. WS проверяет credential при handshake;
закрытый WS требует нового подключения и initialize.

ServeAgents.agent(caller) может создавать пользовательские ресурсы после проверки
credentials при initialize; откладывать их создание до auth/login не нужно.
Проверка владения чатом и разрешений остаётся обязательной для каждой операции.
Обновление прав не должно оставлять навсегда выданные при initialize привилегии.
Истечение/обнаруженный отзыв credential прекращает доступ, включая долгие SSE/WS;
для закэшированных проверок задать конечный срок перепроверки. Новая личность
не может занять существующее соединение, обновлённый токен той же личности может.

Внешние (issuer, subject) сопоставлять внутреннему стабильному ID явно. Совпадение
email, отображаемого login или sub у разных issuer не объединяет пользователей.
Различать human/service identities. Токен не создаёт делегированный Kerberos-билет:
сохранить контракт Credential/NoUserCredential для доступа инструментов.

## 4. ACP-контракт Boba

### 4.1. Пустой authMethods

Оба режима возвращают authMethods: []. Старый boba-oauth, ранее предложенные
boba-login, boba-credentials и boba-browser не объявлять и не использовать.
В примерах успешного обмена нет auth/login, auth/logout или отдельного login URL.

При явном вызове необъявленных auth/login/auth/logout возвращать -32601.
Текущие проверки пустого self._auth_methods в AcpAgent уже соответствуют этому
направлению; не сохранять успешную заглушку login для несуществующего метода.
Клиент AID после initialize сразу переходит к операциям сессий; отсутствие
elicitation.url не мешает доступу к Boba.

### 4.2. Завершение доступа и ошибки

Закрытие ACP-подключения освобождает его ресурсы. Это не отзыв OAuth-токена:
пока credential действителен, клиент может подключиться снова. Выход пользователя
выполняется средствами клиента/системы авторизации: удаление локальных credentials,
отзыв по поддержанному механизму, отключение статического токена. Не выдавать
закрытие SSE или DELETE подключения за глобальный выход из корпоративного SSO.

| Ситуация | Ответ |
| --- | --- |
| Credential отсутствует, неверен или истёк | HTTP 401 с соответствующим WWW-Authenticate до выполнения ACP-запроса |
| Подтверждённой личности не разрешён доступ к службе | HTTP 403 |
| Действительный credential и initialize завершён | session/list/new/resume доступны по правам без login |
| Явно вызваны необъявленные auth/login или auth/logout | JSON-RPC -32601 |
| Подставлен connection ID другого пользователя | HTTP 404, как для неизвестного ID |
| Запрошен чужой чат | Список его не содержит; resume — -32002; delete не меняет чужую историю |

Не возвращать -32000 только потому, что клиент не сделал отменённый второй вход.
Реальные ошибки доступа к внешним ресурсам обрабатывать по ACP, не превращая их
в требование вызвать отсутствующий метод входа Boba.

## 5. Сообщения и получение credentials

Это целевой обмен для реализации, не отчёт о готовности. HTTP показан по
[Streamable HTTP/WS RFD](https://agentclientprotocol.com/rfds/streamable-http-websocket-transport).
Используется HTTPS и HTTP/2; Content-Length вычисляет HTTP-клиент, для HTTP/2
этот заголовок необязателен. Секреты и время в примерах заменены placeholders.

### 5.1. Как клиент узнаёт о защите

Неавторизованный initialize:

~~~http
POST /boba/acp HTTP/2
Host: agent.example.com
Content-Type: application/json
Accept: application/json

{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":2,"info":{"name":"example-client","version":"1"},"capabilities":{}}}
~~~

~~~http
HTTP/2 401 Unauthorized
WWW-Authenticate: Bearer resource_metadata="https://agent.example.com/.well-known/oauth-protected-resource/boba/acp"
Content-Type: application/json
Cache-Control: no-store

{"error":"Authentication required"}
~~~

Запрос не исполнен как ACP, connection ID не создан. Metadata сообщает resource,
разрешённые authorization_servers и передачу токена в header; metadata issuer
сообщает его authorize/token endpoints. Это OAuth discovery, а не authMethods.
Клиенту требуется поддержка выбранного OAuth-сценария либо настройка готового
токена; одно знание ACP не обеспечивает автоматический вход.
[Protected Resource Metadata](https://www.rfc-editor.org/rfc/rfc9728.html#section-5).

Для static-only не объявлять фиктивный OAuth issuer: challenge — Bearer
realm="boba-acp", токен задаётся клиенту при настройке. Basic/Negotiate объявлять
только при включённом соответствующем HTTP-профиле. methodId не подсказывает
клиенту формат HTTP credential.

### 5.2. Человек: OAuth до initialize

1. Клиент имеет/получает обычную OAuth-регистрацию выбранного issuer.
   Регистрация клиентского приложения не означает доверия proxy-утверждениям.
2. Authorization Code + PKCE S256: браузер проходит собственную страницу Boba
   или корпоративный issuer. В Boba local/LDAP вызывает SignIns.by_password,
   Kerberos — SignIns.exchange().handshake.
3. Код приходит callback клиента; клиент обменивает его на access token и
   хранит credentials вне ACP-переписки.
4. Bearer предъявляется при initialize и последующих HTTP-запросах, как в 5.4.

Сохранить LoginPages и существующие backend-проверки. Для собственного OAuth
обеспечить одноразовость code, привязку client/redirect/resource/PKCE и корректные
refresh/rotation/revoke до приёмки. Публичный desktop-клиент не обязан хранить
client_secret; metadata должна отражать фактические способы client authentication.
Не добавлять OAuth password grant.
[OAuth Security BCP](https://www.rfc-editor.org/rfc/rfc9700.html).

### 5.3. Машина от своего имени

Поддержать статический непрозрачный Bearer и OAuth client_credentials. Для своего
OAuth добавить отдельный реестр service clients: нынешний TrustedClient даёт
право proxy-утверждений и не должен автоматически давать иные права.

~~~http
POST /boba/token HTTP/2
Host: agent.example.com
Content-Type: application/x-www-form-urlencoded
Accept: application/json

grant_type=client_credentials&client_id=indexer&client_secret=<CLIENT_SECRET>&resource=https%3A%2F%2Fagent.example.com%2Fboba%2Facp&scope=main
~~~

~~~http
HTTP/2 200 OK
Content-Type: application/json
Cache-Control: no-store
Pragma: no-cache

{"access_token":"<ACCESS_TOKEN>","token_type":"Bearer","expires_in":900,"scope":"main"}
~~~

У внешнего issuer применять его поддержанный client authentication. Результат —
личность службы; клиент не выбирает чужой username. По истечении получается новый
токен; собственный issuer не выдаёт refresh для этого grant. Для статического
Bearer запрос к token endpoint не нужен. JWT не является обязательным форматом.
[Client Credentials](https://www.rfc-editor.org/rfc/rfc6749.html#section-4.4).

### 5.4. Общий обмен после получения Bearer

~~~http
POST /boba/acp HTTP/2
Host: agent.example.com
Authorization: Bearer <ACCESS_TOKEN_OR_STATIC_TOKEN>
Content-Type: application/json
Accept: application/json

{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":2,"info":{"name":"example-client","version":"1"},"capabilities":{}}}
~~~

~~~http
HTTP/2 200 OK
Acp-Connection-Id: conn-1
Content-Type: application/json

{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":2,"info":{"name":"boba-agent","version":"1"},"capabilities":{"session":{}},"authMethods":[]}}
~~~

Реальные capabilities содержат остальные поддержанные возможности Boba.
Клиент открывает поток ответов:

~~~http
GET /boba/acp HTTP/2
Host: agent.example.com
Authorization: Bearer <ACCESS_TOKEN_OR_STATIC_TOKEN>
Acp-Connection-Id: conn-1
Accept: text/event-stream
~~~

~~~http
HTTP/2 200 OK
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
~~~

Затем сразу запрашивает список чатов:

~~~http
POST /boba/acp HTTP/2
Host: agent.example.com
Authorization: Bearer <ACCESS_TOKEN_OR_STATIC_TOKEN>
Acp-Connection-Id: conn-1
Content-Type: application/json

{"jsonrpc":"2.0","id":2,"method":"session/list","params":{}}
~~~

~~~http
HTTP/2 202 Accepted
Content-Length: 0
~~~

В ранее открытом SSE приходит результат:

~~~text
data: {"jsonrpc":"2.0","id":2,"result":{"sessions":[{"sessionId":"chat-1","cwd":"/workspace","title":"Example"}]}}

~~~

Acp-Session-Id здесь не нужен: список относится к соединению, а не к отдельному
чату. Результат ограничен историей подтверждённой личности. Между initialize
и session/list нет другого шага входа. Тот же порядок применяется к new/resume.

### 5.5. Proxy: тот же контракт проверки, что в AID

Доверенный backend сообщает имя пользователя, роли и при настройке профили
в HTTP-заголовках. Переиспользовать цепочку AID:
ProxyRequests.of → SignIns.by_proxy → HmacProxySignIn → общий результат личности
и прав → SessionService. Подпись охватывает login:timestamp:roles:profiles:profile;
проверяются срок и настроенный список адресов. Имена заголовков и источники ролей
берутся из того же ProxyAuthConfig; повторный валидатор в ACP не писать.

Различается только HTTP-артефакт приложения: AID BrowserSessions выдаёт cookie,
а boba serve через существующий ProxyLogin выдаёт access token для API.
Это не auth/login и не отдельное ACP-состояние.

Пример действующего header-обмена службы при включённых roles.header и
profiles.header; X-Remote-User, X-Boba-Timestamp и X-Boba-Signature — стандартные
имена текущего Boba-конфига, не стандарта ACP. Роли/профили также настраиваются.

~~~http
POST /boba/auth/proxy HTTP/2
Host: agent.example.com
Content-Type: application/x-www-form-urlencoded
Accept: application/json
X-Remote-User: alice
X-Remote-Roles: analysts
X-Remote-Profiles: main
X-Boba-Timestamp: <UNIX_SECONDS>
X-Boba-Signature: <HMAC_SHA256_HEX>

client_id=trusted-portal&client_secret=<CLIENT_SECRET>&resource=https%3A%2F%2Fagent.example.com%2Fboba%2Facp
~~~

Для этого примера подписывается строка alice:<UNIX_SECONDS>:analysts:main:
с пустым выбранным профилем. HMAC считать общим ProxySignature, не отдельной
копией алгоритма. ProxyLogin также проверяет OAuth-клиента и разрешение делать
proxy-утверждения; сохранить существующий контракт этого endpoint.

~~~http
HTTP/2 200 OK
Content-Type: application/json
Cache-Control: no-store
Pragma: no-cache

{"access_token":"<ALICE_ACCESS_TOKEN>","token_type":"Bearer","expires_in":900,"scope":"main"}
~~~

Далее выполняется 5.4 с ALICE_ACCESS_TOKEN: история принадлежит Alice.
Заголовки обычного недоверенного клиента не переопределяют личность из Bearer.
Если proxy обслуживает нескольких пользователей, их токены, контексты ACP
и история разделены; токен Bob не позволяет использовать connection ID Alice.

Сохранить также существующий JWT assertion exchange через /boba/token,
grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer. ProxyAssertionVerifier
проверяет client/issuer/audience/resource/срок, SignIns.admit_proxy применяет
общие правила личности и прав. Добавить контроль повторного использования jti.
Это альтернативная упаковка доверенного утверждения, не новый ACP-метод.
AID AgentService уже использует HTTP auth-провайдер ProxyAuth для получения
и обновления Bearer по 401; этот механизм сохранить.

### 5.6. Дополнительные прямые Basic / Negotiate

При явном включении Basic HTTP-адаптер вызывает SignIns.by_password; Negotiate
использует SsoRequests/SsoResponses и SpnegoGate. Проверяемый credential требуется
для каждого HTTP-запроса, включая SSE/DELETE; личность не связывается только с TCP.
После проверки и initialize операции сессий доступны сразу. Эти профили сами
по себе не выпускают JWT и не заменяют основной Bearer-сценарий.

Многошаговый SPNEGO и финальный mutual-auth token должны сохранять контракт
бэкенда; HTTP/2 и WS проверить интеграционно. Не объявлять профиль, который
не реализован. LDAP bind и GSSAPI-проверки остаются в существующих библиотеках.

## 6. Бэкенды, токены и конфигурация

Local/LDAP/Kerberos/proxy остаются в StandardAuthBackends. Проверки static Bearer
и внешних issuer возвращают общий подтверждённый контекст и применяют общие
правила ролей/профилей. OAuth-verifier может жить в boba-oauth; ACP не должен
зависеть от TokenClaims одного issuer. CallerTokens обобщить до адаптера такого
контекста, не добавляя в него парсеры паролей, билетов или proxy-подписей.

Static Bearer: случайный высокоэнтропийный секрет, хранение digest, constant-time
сравнение, token ID, стабильная service identity, роли/профили, срок, отключение
и ротация. Не превращать пароль local-пользователя в статический токен.

Внешние JWT: разрешённые issuer, audience, алгоритмы, подпись через настроенные
JWKS и сроки. Opaque tokens: настроенная introspection. Непроверенный iss может
выбирать только заранее разрешённый verifier; нельзя отправлять секреты по
URL из непроверенных iss/jku или перебирать чужие introspection endpoints.
Не принимать ID token вместо access token. Реестр static tokens и OAuth verifiers
должен давать однозначный результат без обхода отказа более слабой проверкой.

Новые настройки разместить в agent.authentication. Это целевая схема, а не
уже доступный конфиг:

| Поле | Смысл |
| --- | --- |
| transport | Разрешённые bearer/basic/negotiate; по умолчанию bearer |
| builtin_oauth.enabled | Собственный OAuth независимо от проверки внешних issuer |
| external_oauth[] | id, issuer, resource/audience, jwt/introspection, доверенные endpoints, mapping личности/прав |
| static_bearer[] | token ID, digest, service identity, grants, expiry/disabled |
| service_clients | Отдельные client_credentials-регистрации для собственной личности службы |
| credential_cache_sec | Максимальная задержка перепроверки/отзыва для opaque/static и долгих потоков |

Настроек browser-входа через ACP, purpose=interactive и отдельного ACP auth state
не вводить. Несколько issuer могут работать одновременно. External-only/static
не требуют собственного OAuth signing secret; ключи включённого Kerberos/sealing
сохраняются. Существующий agent.clients остаётся proxy-регистром либо мигрируется
под понятное имя с диагностикой, без молчаливого расширения прав. Конфиг auth
бэкендов переиспользовать, вторую таблицу local/LDAP/proxy в agent не создавать.

## 7. Изменения использования SDK

### 7.1. Публичные расширения

AgentSide.tool_source перевести на AgentSideConnection.send_extension_request.
Приём _boba/tool_source перенести в handle_extension_request клиентского
обработчика. Общую обработку расширений не дублировать в ClientConnection.
Проверки собственных payload сохранить. Современный MCP-адаптер пока отделить,
но не удалять: rc3 ожидает connectionId вместо serverId/requestId.

### 7.2. URL-elicitation через готовый SDK

Сохранить задачу расширения ClientLink для URL-elicitation и complete_elicitation
через AgentSideConnection, включая перенос между event loop через LoopCall.
Назначение — внешние взаимодействия агента/инструментов в ходе работы; допуск
к самому boba serve уже установлен HTTP-аутентификацией.

Для текущих session-scoped взаимодействий передавать sessionId. Request-scoped
вариант использует requestId реального ещё не завершённого запроса на этом
соединении; оба поля одновременно недопустимы. Специальный адаптер захвата RPC id
ради отсутствующего auth/login и маршрут /login/acp не входят в это задание.
Если появится отдельный потребитель request-scoped API, интеграцию контекста
проектировать для него, не используя общий «последний запрос» или счётчик.

Boba-aid объявляет elicitation.url только при фактической поддержке: показывает
домен и сообщение, получает согласие и возвращает accept/decline/cancel.
Accept означает согласие открыть URL, не успешную внешнюю авторизацию. Секреты
не собираются формой и не возвращаются в ACP/model context. Состояние внешнего
взаимодействия привязано к текущему пользователю и соединению; завершение URL
не меняет владельца ACP-контекста. Отсутствие URL-capability не блокирует доступ
к Boba по Bearer. [Elicitation](https://agentclientprotocol.com/protocol/v2/elicitation).

### 7.3. Stdio и оставшиеся несовместимости

Проверить все вызовы BatchTransport и тесты. Целевой stdio — один объект на строку;
убрать сборку пакетных ответов. На массив отвечать отказом -32600 без исполнения
элементов. Сохранить -32700 на malformed JSON, ограничения размера и чистый stdout.
Поскольку NdjsonTransport rc3 пропускает часть некорректных данных без ответа,
оставить минимальный framing-адаптер, если замена на него меняет эти гарантии.
Существующий отдельный output_fd нужен для защиты протокола от чужих print.

В AgentSide сохранить проверки отсутствующих params, replayFrom и выбора версии
до устранения соответствующих расхождений SDK. Отдельно исправить повторный
initialize, разрешённый текущим V2 до первого другого запроса: учесть SDK
InitializationState и HTTP AcpServer, который сейчас создаёт новый connection ID.
Повтор должен сохранять соединение и обновлять capabilities; неуспешный повтор
не стирает предыдущее согласование. Сверить с закреплённым нормативным срезом:
[Initialization](https://agentclientprotocol.com/protocol/v2/initialization).

AcpServer и handle_websocket оставить ответственными за транспорт. Не заменять
AcpEndpoint на create_asgi_app ценой удаления ownership, timeout и cleanup.
Contrib helpers rc3 используют модели V1; в код V2 их напрямую не переносить.

## 8. Порядок реализации

1. Зафиксировать pin схем и новый wire-контракт: authMethods пуст, сразу после
   initialize разрешены операции сессий по проверенной личности. Убрать объявление
   boba-oauth; проверить отсутствие обязательного login в AID и тестах.
2. Перевести расширения на публичный API SDK, упростить stdio, сохранить нужные
   обходы несовместимости. Проверить HTTP/WS, MCP-over-ACP и остановку процесса.
3. Обобщить подтверждённый контекст и владельца соединения; убрать зависимость
   endpoint от собственных TokenClaims, сохранить изоляцию истории и права.
4. Сохранить человеческий OAuth-вход и proxy по общим бэкендам AID/boba-auth;
   добавить static Bearer, внешние issuer и отдельный client_credentials grant.
5. Завершить политику expiry/revocation/rotation и дополнительные явно включённые
   Basic/SPNEGO профили; обновить конфигурацию и клиентские примеры.
6. Подключить URL-elicitation через SDK к внешним взаимодействиям в сессиях;
   выполнить приёмку без добавления отдельного ACP-входа.

Наличие обработчика/модели не означает завершённую реализацию. Обход SDK должен
воспроизводиться через зависимость или изолированный адаптер; ручная правка
.venv не является изменением проекта.

## 9. Приёмка

| Проверка | Требуемый результат |
| --- | --- |
| Объявление методов | authMethods: [] в serve и stdio; старые и предложенные методы входа отсутствуют |
| Работа сразу после initialize | list/new/resume доступны по правам без login; отсутствие URL-capability не мешает |
| Необъявленные login/logout | Явный вызов получает -32601; нет успешной заглушки или скрытого второго допуска |
| Нет/неверный/истёкший credential | HTTP 401 с challenge до обработки ACP; токен есть, но прав нет — 403 |
| Человек: local/LDAP | OAuth-вход использует существующий SignIns, выдаёт Bearer; история вошедшего человека |
| Человек: Kerberos | Общий SpnegoGate, существующие правила делегирования, затем обычный доступ к ACP |
| Машина: static Bearer | Работает без браузера, OAuth-сервера и proxy, со своей service identity |
| Машина: client_credentials | Работает со своим и корпоративным issuer; нельзя выбрать чужого пользователя |
| Proxy: заголовки как в AID | Общие ProxyRequests/SignIns; логин, роли, профили и подпись имеют тот же смысл; после обмена Bearer сразу работает с ACP |
| Proxy: отказ и изоляция | Подмена имени/ролей/профилей, неверная подпись, просрочка и запрещённый источник отвергаются; история не становится общей для proxy |
| Proxy: assertion | Существующий обмен работает, чужие client/resource и повтор jti отвергаются |
| Несколько issuer | Принимаются настроенные issuer; неверные подпись/audience/срок и ID token отвергаются; личности не склеиваются по login |
| Собственный OAuth | Одноразовый code, PKCE/redirect/client/resource, refresh rotation и реальный отзыв |
| Basic/Negotiate при включении | HTTP/SSE/WS используют общие бэкенды без браузера; нет зависимости личности от TCP |
| История и connection ID | Чужой credential не использует контекст; чужие чаты не выдаются и не изменяются |
| Переподключение | Действительный Bearer позволяет восстановить поток; после удаления контекста — initialize/resume без ACP login |
| Закрытие и отзыв | Закрытие соединения освобождает ресурсы, но не выдаётся за отзыв токена; expiry/revocation ограничивают и долгие SSE/WS |
| AID | Сохраняется HTTP ProxyAuth и обновление Bearer; нет обязательного login при пустом authMethods |
| URL-elicitation | Проверена capability, SDK-корреляция, accept/decline/cancel и завершение; токены не уходят в ACP; личность Boba не меняется |
| Расширения и stdio | Публичный API, один объект на строку, корректные ошибки, чистый stdout; пакет не исполняется |
| V2 | Выбор версии, params, replayFrom, повтор initialize, отсутствие случайных моделей V1 |

Использовать существующие тесты:
[transport/test_acp_protocol.py](../packages/infra/transport/boba-transport-acp/tests/test_acp_protocol.py),
[test_cli_serve.py](../packages/apps/boba-cli/tests/test_cli_serve.py),
[test_cli_acp.py](../packages/apps/boba-cli/tests/test_cli_acp.py),
[test_cli_shutdown.py](../packages/apps/boba-cli/tests/test_cli_shutdown.py),
[test_cli_tck.py](../packages/apps/boba-cli/tests/test_cli_tck.py),
а также наборы boba-auth, boba-oauth и AID. LDAP/Kerberos проверять на настроенном
стенде; подмена backend подтверждает только маршрутизацию вызовов.

Проверка JSON Schema не заменяет проверки последовательности сообщений,
ownership, HTTP-аутентификации и отсутствия второго ACP-входа. Обновить Frames/acp_schema при смене pin:
[acp_frames.py](../packages/testing/boba-stand/src/boba/stand/acp_frames.py) сейчас
ссылается на отдельный срез TCK. В отчёте различать пройденные проверки,
непройденные и недоступные из-за окружения; не объявлять полное соответствие
по одним мокам или успешной сериализации.
