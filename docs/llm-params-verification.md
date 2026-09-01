# Как проверить, что параметры LLM реально доехали до модели

Документ про диагностику: как убедиться, что `temperature`, `top_k`,
`reasoning_effort` и прочее действительно применились, как выяснить, какие
параметры вообще понимает сервер и шаблон конкретной модели, и что каждый из
них делает с ответом. Примеры сняты на ollama 0.32.13 и `qwen3.8:27b-q4_K_M`,
но метод переносится на vLLM, SGLang и llama.cpp.

## Источники

Все утверждения про поведение ollama проверены по исходникам и закреплены за
коммитом [`ef117cfcc0c4`](https://github.com/ollama/ollama/tree/ef117cfcc0c4) (2026-09-01) —
ссылки в тексте ведут на конкретные строки именно этой ревизии. Если поведение
разошлось с описанным, сравнивайте с текущим `main`: файлы те же.

| Файл | Что там лежит |
|---|---|
| [`api/types.go`](https://github.com/ollama/ollama/blob/ef117cfcc0c4/api/types.go) | структуры нативного API: `ChatRequest`, `Options`, `ThinkValue`, `ChatResponse` |
| [`openai/openai.go`](https://github.com/ollama/ollama/blob/ef117cfcc0c4/openai/openai.go) | структуры `/v1` и перевод в нативные опции: `ChatCompletionRequest`, `FromChatRequest`, `thinkFromReasoningEffort` |
| [`middleware/openai.go`](https://github.com/ollama/ollama/blob/ef117cfcc0c4/middleware/openai.go) | разбор тела `/v1` и конвертация ответа: `ChatMiddleware`, `ChatWriter` |
| [`server/routes.go`](https://github.com/ollama/ollama/blob/ef117cfcc0c4/server/routes.go) | обработчик `/api/chat`, `_debug_render_only`, сборка опций `modelOptions` |
| [`model/renderers/qwen35.go`](https://github.com/ollama/ollama/blob/ef117cfcc0c4/model/renderers/qwen35.go) | рендерер промпта Qwen3.5/3.8: тексты reasoning-инструкций и сборка turn'ов |
| [`model/renderers/renderer.go`](https://github.com/ollama/ollama/blob/ef117cfcc0c4/model/renderers/renderer.go) | реестр: имя `RENDERER` из Modelfile → конкретный рендерер |

Шаблоны Qwen для vLLM/SGLang берутся не отсюда, а из репозиториев моделей:
[Qwen3.8-27B/chat_template.jinja](https://huggingface.co/Qwen/Qwen3.8-27B/blob/main/chat_template.jinja)
и [Qwen3.6-27B/chat_template.jinja](https://huggingface.co/Qwen/Qwen3.6-27B/blob/main/chat_template.jinja);
рекомендованные сэмплеры и описание `reasoning_effort` — в карточках тех же
моделей.

---

## Часть 0. Проблема: параметр исчезает молча

Ситуация. Администратор дописал в профиль:

```toml
[profiles.general.sampling]
    temperature = 1.0
    top_k       = 20
```

Приложение стартовало без ошибок, модель ответила. Кажется, что `top_k`
применился. На самом деле его никто не видел: openai-совместимый слой ollama
не знает такого поля запроса и выбросил его при разборе тела — без ошибки,
без предупреждения, без записи в лог.

Так ведёт себя вся цепочка. Параметр может потеряться на трёх разных
уровнях, и на каждом молчание выглядит одинаково — как успех.

| Уровень | Кто отсеивает | Что происходит с лишним |
|---|---|---|
| 1. Наш провайдер | `boba.llm.*_chat` собирает тело запроса | ключ уходит в тело как есть — здесь не теряется ничего |
| 2. Парсер сервера | структура запроса ollama / vLLM | неизвестное поле молча отбрасывается |
| 3. Шаблон промпта | рендерер модели | переменная, которой шаблон не объявил, игнорируется |

Отсюда правило: **не верьте конфигу, смотрите на то, что реально ушло по
проводу и что попало в промпт.** Ниже — три инструмента ровно под эти три
уровня.

---

## Часть 1. Что ушло из boba: дамп HTTP-обмена

Таблица `sampling` профиля уходит в тело запроса без переименований — это
контракт (`ChatRequest.sampling`). Значит первый шаг — увидеть тело своими
глазами.

Включается в секции транспорта:

```toml
[http]
    dump         = { enable = true, path = "${env.data}/dump" }
    max_retries  = 2
    read_timeout = 600
```

Секция `[http]` в конфиге одна: это поведение транспорта, общее для всех
провайдеров. Адрес (`base_url`, `api_key`) живёт в провайдере профиля, так
что включённый дамп сразу действует и на openai-профиль, и на ollama.

Каждый обмен пишется в файл `<host>.log`. Реальный кусок такого дампа:

```
20:30:47.127591: 127.0.0.1:49218 >>> 127.0.0.1:11434:
POST /api/chat HTTP/1.1
Host: localhost:11434
Authorization: Bearer ollama
Content-Length: 175
Content-Type: application/json

{"model":"qwen3.8:27b-q4_K_M","messages":[{"role":"user","content":"скажи ровно: привет"}],"stream":false,"think":"low","options":{"num_predict":8,"top_k":20}}

20:32:32.906288: 127.0.0.1:11434 <<< 127.0.0.1:49218:
HTTP/1.1 200 OK
Content-Type: application/json; charset=utf-8

{"model":"qwen3.8:27b-q4_K_M","created_at":"2026-09-01T17:32:32.905847145Z","mess…
```

Что здесь читаем:

- `think` и `options` уехали — значит уровень 1 пройден;
- если параметра в теле нет, дальше искать бессмысленно: проблема в конфиге
  (опечатка в имени ключа, не та секция, не тот профиль).

Дамп — единственное место, где видно тело целиком, вместе с заголовками и
адресом. Для одноразовой проверки его можно включить и выключить обратно,
для постоянного разбора он живёт в `${env.data}/dump`.

Если поднимать приложение ради проверки не хочется, тот же путь проходится
скриптом — провайдер собирает тело ровно так же, как в бою:

```python
import asyncio

from boba.chat.http import HttpConfig, HttpDumpConfig
from boba.chat.provider import ChatRequest, ChatRole, ChatTurn, OllamaChatConfig
from boba.llm.ollama_chat import OllamaChatProvider
from boba.llm.http import LlmHttp


async def main() -> None:
    cfg = OllamaChatConfig(
        kind="ollama",
        http=HttpConfig(dump=HttpDumpConfig(enable=True, path="/tmp/llm-dump")),
        base_url="http://localhost:11434",
        api_key="ollama",
    )
    async with LlmHttp.client(cfg.http) as client:
        provider = OllamaChatProvider(cfg, client, "qwen3.8:27b-q4_K_M")
        request = ChatRequest(
            messages=[ChatTurn(role=ChatRole.USER, content="скажи ровно: привет")],
            sampling={"think": "low", "options": {"num_predict": 8, "top_k": 20}},
            stream=False,
        )
        async for event in provider.chat(request):
            print(event)


asyncio.run(main())
```

Тело запроса ляжет в `/tmp/llm-dump/localhost.log`. Подставьте в `sampling`
ровно ту таблицу, что стоит в профиле, — и увидите, во что она превращается
на проводе.

---

## Часть 2. Что принял сервер: предупреждения ollama

Дальше тело разбирает сервер, и вот тут поведение зависит от того, **куда**
положен ключ.

### Внутри `options` — предупреждение в логе

Блок `options` разбирается методом [`Options.FromMap`](https://github.com/ollama/ollama/blob/ef117cfcc0c4/api/types.go#L1001),
и он жалуется на незнакомый ключ ([types.go:1017](https://github.com/ollama/ollama/blob/ef117cfcc0c4/api/types.go#L1017)):

```bash
curl -s localhost:11434/api/chat -d '{
  "model": "qwen3.8:27b-q4_K_M",
  "messages": [{"role":"user","content":"скажи ровно: привет"}],
  "options": {"num_predict": 40, "top_k": 20, "bogus_option": 123}
}'

docker logs ollama --since 3m | grep "invalid option"
```

```
time=2026-09-01T17:28:56.979Z level=WARN source=types.go:1007 msg="invalid option provided" option=bogus_option
```

Ошибки нет — запрос выполнится, — но в логе останется след. Это самый
дешёвый способ поймать опечатку в имени сэмплера.

Обратите внимание: жалоба возникает только на **неизвестный** ключ. Неверный
*тип* значения — уже ошибка запроса: `option "top_k" must be of type integer`
(там же, ветками ниже по `FromMap`).

### На верхнем уровне — полное молчание

А вот `top_k`, положенный не в `options`, а рядом с `model` и `messages`,
исчезает бесследно:

```bash
curl -s localhost:11434/api/chat -d '{
  "model": "qwen3.8:27b-q4_K_M",
  "messages": [{"role":"user","content":"hi"}],
  "top_k": 20,
  "options": {"num_predict": 16}
}'

docker logs ollama --since 60s | grep -c "invalid option"
# 0
```

Причина в устройстве разбора: тело читает gin (`c.ShouldBindJSON`), то есть
`encoding/json` без `DisallowUnknownFields`. Так устроены обе двери —
[нативный ChatHandler](https://github.com/ollama/ollama/blob/ef117cfcc0c4/server/routes.go#L2440-L2450) и
[openai-совместимый ChatMiddleware](https://github.com/ollama/ollama/blob/ef117cfcc0c4/middleware/openai.go#L446-L453).
Поля, которого нет в структуре запроса, для сервера просто не существует.
Никто не ошибается, никто не предупреждает — параметр тихо не работает.

**Практический вывод.** Верхний уровень тела — закрытый список полей;
`options` — словарь, который хотя бы ругается. Если параметр «не действует» и
в логе тишина, скорее всего он лежит не там, где нужно.

---

## Часть 3. Что попало в промпт: `_debug_render_only`

Последний уровень — шаблон. Здесь параметр уже принят сервером, но может не
дойти до текста промпта. Для этого у нативного API есть режим «отрендерь и
покажи, не запуская модель»:

```bash
curl -s localhost:11434/api/chat -d '{
  "model": "qwen3.8:27b-q4_K_M",
  "messages": [{"role":"user","content":"hi"}],
  "think": "low",
  "stream": false,
  "_debug_render_only": true
}' | jq -r '._debug_info.rendered_template'
```

```
<|im_start|>system
Reasoning effort is set to low. Keep your thinking brief and focused, moving directly to the conclusion without unnecessary elaboration.<|im_end|>
<|im_start|>user
hi<|im_end|>
<|im_start|>assistant
<think>
```

Вот тот же запрос с другими значениями `think` — видно, что именно меняется
в промпте:

| `think` | Отрендеренный промпт |
|---|---|
| `"low"` | system-turn с инструкцией `Reasoning effort is set to low…`, затем `<think>\n` |
| `"medium"` | system-turn **отсутствует**, только `<think>\n` |
| `"high"`, `"max"` | system-turn с инструкцией `Reasoning effort is set to xhigh…` |
| `true` | то же, что medium: `<think>\n` без инструкции |
| `false` | `<think>\n\n</think>\n\n` — пустой блок, думать негде |
| `"xhigh"` | **ошибка 400** — см. ниже |

Обратите внимание на две неочевидные вещи.

Во-первых, `medium` — это не «средняя инструкция», а **отсутствие**
инструкции. В шаблоне для него не предусмотрено текста, поэтому промпт
совпадает с `think: true`.

Во-вторых, `think: false` не просто «выключает размышления»: он
**префиллит** пустой блок `<think></think>`, физически лишая модель места для
рассуждений. Это сильнее, чем просьба не думать.

Отдельная ловушка — значение `xhigh`. В документации Qwen это имя дефолтного
уровня, и через `/v1` оно работает. А нативный `/api/chat` его не принимает:

```json
{"error":"invalid think value: \"xhigh\" (must be \"high\", \"medium\", \"low\", \"max\", true, or false)"}
```

Список допустимых значений задан в [`ThinkValue.IsValid`](https://github.com/ollama/ollama/blob/ef117cfcc0c4/api/types.go#L1142-L1155),
сообщение об ошибке — в [types.go:1225](https://github.com/ollama/ollama/blob/ef117cfcc0c4/api/types.go#L1225).

Шкала `think` у ollama своя и не совпадает с именами Qwen. Максимальный
уровень тут называется `high` или `max`, а инструкция в промпт при этом
подставляется именно xhigh-шная. Через `/v1` слово `xhigh` тоже не доезжает
до рендерера дословно — оно клампится в `max` ещё на входе.

Ключевая оговорка: `_debug_info` возвращает только нативный `/api/chat`
([routes.go:2708](https://github.com/ollama/ollama/blob/ef117cfcc0c4/server/routes.go#L2708-L2718)).
Через `/v1/chat/completions` ответ проходит конвертацию в openai-формат, и
поле теряется. Проверять рендер надо нативным эндпоинтом, даже если само
приложение ходит по `/v1`.

---

## Часть 4. Как узнать, какие параметры поддерживает ollama

Документация отстаёт, поэтому источник истины — исходники. Проверка занимает
три шага: **объявлено → прочитано → использовано**. Параметр работает,
только если прошёл все три.

### Нативный `/api/chat`

**Шаг 1: объявлено?** Структура [`ChatRequest`](https://github.com/ollama/ollama/blob/ef117cfcc0c4/api/types.go#L133-L180)
в `api/types.go`. Верхний уровень тела:

```go
type ChatRequest struct {
    Model    string           `json:"model"`
    Messages []Message        `json:"messages"`
    Stream   *bool            `json:"stream,omitempty"`
    Format   json.RawMessage  `json:"format,omitempty"`
    KeepAlive *Duration       `json:"keep_alive,omitempty"`
    Tools                     `json:"tools,omitempty"`
    Options  map[string]any   `json:"options"`
    Think    *ThinkValue      `json:"think,omitempty"`
    Truncate *bool            `json:"truncate,omitempty"`
    Shift    *bool            `json:"shift,omitempty"`
    DebugRenderOnly bool      `json:"_debug_render_only,omitempty"`
    Logprobs bool             `json:"logprobs,omitempty"`
    …
}
```

Всё, чего в этом списке нет, на верхнем уровне бессмысленно.

Сэмплеры живут в [`Options`](https://github.com/ollama/ollama/blob/ef117cfcc0c4/api/types.go#L568-L585) (плюс
встроенный [`Runner`](https://github.com/ollama/ollama/blob/ef117cfcc0c4/api/types.go#L588-L596) с `num_ctx` и соседями) — полный список:

```go
type Options struct {
    Runner                            // num_ctx, num_batch, num_gpu, num_thread, …
    NumKeep          int      `json:"num_keep,omitempty"`
    Seed             int      `json:"seed,omitempty"`
    NumPredict       int      `json:"num_predict,omitempty"`
    TopK             int      `json:"top_k,omitempty"`
    TopP             float32  `json:"top_p,omitempty"`
    MinP             float32  `json:"min_p,omitempty"`
    TypicalP         float32  `json:"typical_p,omitempty"`
    RepeatLastN      int      `json:"repeat_last_n,omitempty"`
    Temperature      float32  `json:"temperature,omitempty"`
    RepeatPenalty    float32  `json:"repeat_penalty,omitempty"`
    PresencePenalty  float32  `json:"presence_penalty,omitempty"`
    FrequencyPenalty float32  `json:"frequency_penalty,omitempty"`
    Stop             []string `json:"stop,omitempty"`
}
```

**Шаг 2: прочитано?** Для `options` это [`FromMap`](https://github.com/ollama/ollama/blob/ef117cfcc0c4/api/types.go#L1001) —
он же печатает `invalid option provided`. Для верхнего уровня — код обработчика
[`ChatHandler`](https://github.com/ollama/ollama/blob/ef117cfcc0c4/server/routes.go#L2440).

**Шаг 3: использовано?** Для `think` — [рендерер модели](https://github.com/ollama/ollama/blob/ef117cfcc0c4/model/renderers/qwen35.go#L113-L132)
(см. часть 5).

### Openai-совместимый `/v1/chat/completions`

Здесь список полей заметно короче — структура
[`ChatCompletionRequest`](https://github.com/ollama/ollama/blob/ef117cfcc0c4/openai/openai.go#L105-L128). Перевод в нативные
опции делает [`FromChatRequest`](https://github.com/ollama/ollama/blob/ef117cfcc0c4/openai/openai.go#L549) в том же файле
(сама раскладка `options` — [openai.go:641-679](https://github.com/ollama/ollama/blob/ef117cfcc0c4/openai/openai.go#L641-L679)):

| Поле `/v1` | Во что превращается |
|---|---|
| `max_tokens` | `options.num_predict` |
| `temperature` | `options.temperature` (без множителей; если не задано — 1.0) |
| `top_p` | `options.top_p` (если не задано — 1.0) |
| `presence_penalty` | `options.presence_penalty` |
| `frequency_penalty` | `options.frequency_penalty` |
| `seed`, `stop` | `options.seed`, `options.stop` |
| `reasoning_effort` (или `reasoning.effort`) | `ThinkValue` → рендерер |

**Чего в этой структуре нет вообще:** `top_k`, `min_p`, `repeat_penalty`,
`num_ctx`, `keep_alive`. Через `/v1` их передать нельзя никак — это и есть
исходная ловушка из части 0. Есть два выхода: нативный провайдер
(`kind = "ollama"`) или запекание значений в модель через Modelfile.

Смягчающее обстоятельство: официальные сборки часто уже несут рекомендованные
производителем модели значения (у `qwen3.8:27b-q4_K_M` это `top_k 20`,
`min_p 0`, `repeat_penalty 1` — см. часть 5). То есть через `/v1` вы не
столько «теряете» их, сколько не можете переопределить.

Шкала `reasoning_effort` на входе `/v1` шире, чем у ollama, и клампится —
[`thinkFromReasoningEffort`](https://github.com/ollama/ollama/blob/ef117cfcc0c4/openai/openai.go#L531-L546):

| Прислали | `think` внутри | Итог для qwen3.8 |
|---|---|---|
| не задан | `nil` | инструкция xhigh (дефолт модели) |
| `none` | `false` | пустой `<think></think>`, размышлений нет |
| `minimal`, `low` | `low` | инструкция low |
| `medium` | `medium` | инструкции нет |
| `high`, `max`, `xhigh`, `ultra` | `max` | инструкция xhigh |

Обратите внимание на предпоследнюю строку: `xhigh` и `ultra` понимает именно
openai-слой, который сводит их к `max`. Нативный `/api/chat` те же слова
отвергает с ошибкой — шкалы у двух эндпоинтов разные.

Неизвестное значение — единственный случай, когда `/v1` отвечает ошибкой, а
не молчанием:

```json
{"error":{"message":"invalid reasoning value: \"turbo\" (must be \"minimal\", \"low\", \"medium\", \"high\", \"xhigh\", \"ultra\", \"max\", or \"none\")","type":"invalid_request_error"}}
```

---

## Часть 5. Как узнать, что понимает шаблон конкретной модели

Даже поддержанный сервером параметр может ничего не делать: решает шаблон
промпта. Начинать надо с `ollama show --modelfile`:

```
FROM /root/.ollama/models/blobs/sha256-f5f1dd89…
TEMPLATE {{ .Prompt }}
RENDERER qwen3.8
PARSER qwen3.5
PARAMETER min_p 0
PARAMETER presence_penalty 0
PARAMETER repeat_penalty 1
PARAMETER temperature 1
PARAMETER top_k 20
PARAMETER top_p 0.95
```

Читается так:

- **`RENDERER qwen3.8`** — промпт собирает не jinja-шаблон, а Go-код ollama:
  [`newQwen38Renderer`](https://github.com/ollama/ollama/blob/ef117cfcc0c4/model/renderers/qwen35.go#L61-L69) в
  `model/renderers/qwen35.go`; имя из Modelfile сопоставляется с рендерером в
  [реестре renderer.go](https://github.com/ollama/ollama/blob/ef117cfcc0c4/model/renderers/renderer.go#L69-L73). Поэтому у
  ollama нет и не может быть `chat_template_kwargs`: переменных шаблона тут
  просто не существует, вместо них — поля структуры запроса.
- **`PARSER qwen3.5`** — тем же семейством разбирается ответ модели (блоки
  `<think>` и `<tool_call>` превращаются в `message.thinking` и
  `message.tool_calls`).
- **`TEMPLATE {{ .Prompt }}`** — заглушка: вся работа у рендерера.
- **`PARAMETER …`** — дефолты, которые модель несёт с собой. У этой сборки
  уже прописаны рекомендованные Qwen значения (`top_k 20`, `min_p 0`,
  `repeat_penalty 1`, `temperature 1`, `top_p 0.95`). Опции запроса
  перекрывают их: порядок сборки — дефолты ollama → `PARAMETER` из
  Modelfile → `options` запроса
  ([modelOptions, routes.go:125-148](https://github.com/ollama/ollama/blob/ef117cfcc0c4/server/routes.go#L125-L148)).

То есть если нужно зафиксировать сэмплеры, недоступные через `/v1`, их можно
запечь в собственную сборку модели:

```
FROM qwen3.8:27b-q4_K_M
PARAMETER top_k 20
PARAMETER min_p 0.0
```

```bash
ollama create qwen3.8-boba -f Modelfile
```

Для **vLLM/SGLang** картина другая: там промпт собирает настоящий
`chat_template.jinja` из репозитория модели на HuggingFace, и проверять надо
его. Открываете файл и смотрите, какие переменные он читает — `enable_thinking`,
`preserve_thinking`, `reasoning_effort`. То, чего в шаблоне нет, передавать
бесполезно: vLLM отфильтрует неизвестные ключи `chat_template_kwargs` по
списку объявленных в шаблоне переменных.

Разница между поколениями Qwen как раз в этом:

| Переменная | Qwen3.6 | Qwen3.8 |
|---|---|---|
| `enable_thinking` | есть, по умолчанию `true` | есть, по умолчанию `true` |
| `preserve_thinking` | есть, по умолчанию **`false`** | есть, по умолчанию **`true`** |
| `reasoning_effort` | **нет вообще** | `xhigh` (дефолт), `medium`, `low` |

У Qwen3.6 глубину размышлений регулировать нечем — только «думать / не
думать», потолок ответа и просьба в system-промпте. Через ollama
`reasoning_effort` для 3.6 вырождается в булев переключатель: рендерер
`qwen3.5` читает из `ThinkValue` только «да/нет», градация игнорируется.

---

## Часть 6. Что эти параметры делают с ответом

### Глубина размышлений

Механизм у Qwen3.8 чисто текстовый: в system-turn подставляется одна из двух
фраз (или ни одной) — сами строки лежат константами в
[qwen35.go:15-16](https://github.com/ollama/ollama/blob/ef117cfcc0c4/model/renderers/qwen35.go#L15-L16), выбор делает
[`qwen38ReasoningInstructions`](https://github.com/ollama/ollama/blob/ef117cfcc0c4/model/renderers/qwen35.go#L113-L132).
Никакого «бюджета токенов» под капотом нет — модель обучена реагировать на эту
формулировку.

- **xhigh** (дефолт): `Reasoning effort is set to xhigh. Please think
  carefully through the task, validate key assumptions, consider plausible
  alternatives, and prioritize correctness, consistency, and clarity in the
  final answer.`
- **low**: `Reasoning effort is set to low. Keep your thinking brief and
  focused, moving directly to the conclusion without unnecessary
  elaboration.`
- **medium**: инструкции нет — поведение модели «по умолчанию обученное».

Замер на одной и той же задаче (простая арифметика: «3 яблока и 4 груши,
добавили 5 яблок, сколько всего», `num_predict = 2000`, `temperature = 1.0`):

| `think` | `eval_count` | длина `thinking` | длина ответа |
|---|---|---|---|
| `low` | 108 | 279 симв. | 54 симв. |
| `medium` | 202 | 558 симв. | 75 симв. |
| `high` (= xhigh) | 123 | 273 симв. | 82 симв. |

Цифры показательны сразу в двух смыслах. Разница между `low` и `medium`
двукратная — инструкция работает. А вот `high` оказался короче `medium`, и
это не парадокс, а **предупреждение о методике**: на тривиальной задаче
длина размышлений упирается не в инструкцию, а в то, что думать особо не о
чем, и разброс при `temperature = 1.0` перекрывает эффект.

Отсюда два правила замера:

1. берите задачу, которая действительно требует рассуждения (перебор,
   несколько шагов, проверка гипотез) — на «сколько будет 17×3» вы измерите
   шум;
2. повторяйте прогон несколько раз или фиксируйте `seed`; один запуск на
   один уровень ничего не доказывает.

Скрипт замера — три запроса подряд, сравниваем `eval_count` и длину
`message.thinking`:

```python
import json
import urllib.request

Q = "Найди все трёхзначные числа, равные сумме факториалов своих цифр."

for think in ("low", "medium", "high"):
    body = {
        "model": "qwen3.8:27b-q4_K_M",
        "messages": [{"role": "user", "content": Q}],
        "think": think,
        "stream": False,
        "options": {"num_predict": 4000, "num_ctx": 8192, "seed": 7},
    }
    req = urllib.request.Request(
        "http://localhost:11434/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    reply = json.load(urllib.request.urlopen(req, timeout=3600))
    print(think, reply["eval_count"], len(reply["message"].get("thinking", "")))
```

Если цифры по уровням совпали — параметр не доехал; возвращайтесь к части 3
и посмотрите отрендеренный промпт.

Практические следствия:

- рассуждения тратят токены из того же потолка, что и ответ. `num_predict`
  (он же `max_tokens`) ограничивает **сумму**. С xhigh на длинной задаче
  ответ может оборваться на середине размышлений — увеличивайте потолок или
  снижайте усилие;
- в многошаговых агентских сценариях низкое усилие не всегда быстрее: Qwen
  прямо предупреждает, что недостаточный анализ ведёт к ошибкам, повторам и
  росту общего числа токенов;
- `preserve_thinking` влияет не на длину одного ответа, а на историю: с ним
  блоки `<think>` прошлых ходов остаются в контексте. Это дороже по токенам
  на ход, но улучшает связность решений и попадание в KV-кэш.

### Сэмплеры

Qwen рекомендует разные наборы для двух режимов, и это не косметика —
инструктивный режим требует штрафа за повтор, которого в thinking-режиме
быть не должно:

| Параметр | Thinking | Instruct (без размышлений) |
|---|---|---|
| `temperature` | 1.0 | 0.7 |
| `top_p` | 0.95 | 0.80 |
| `top_k` | 20 | 20 |
| `min_p` | 0.0 | 0.0 |
| `presence_penalty` | 0.0 | 1.5 |
| `repeat_penalty` | 1.0 | 1.0 |

Для точного кодинга (WebDev) Qwen3.6 рекомендует `temperature = 0.6` при
прочих равных.

---

## Часть 7. Как это выглядит в конфиге boba

У нас два удалённых провайдера, и они отличаются именно словарём параметров.

**openai-совместимый** — плоская таблица, имена openai:

```toml
[profiles.general]
    model = "deepseek/deepseek-v4-flash"
    [profiles.general.provider]
        kind     = "openai"
        http     = "${http}"
        base_url = "${site.llm_url}"
        api_key  = "${site.llm_token}"
    [profiles.general.sampling]
        temperature      = 1.0
        top_p            = 0.95
        presence_penalty = 0.0
        max_tokens       = 4096
        reasoning_effort = "low"
```

**нативный ollama** — `think` верхним уровнем, сэмплеры в `options`, имена
ollama:

```toml
[profiles.ollama]
    model = "qwen3.8:27b-q4_K_M"
    [profiles.ollama.provider]
        kind     = "ollama"
        http     = "${http}"
        base_url = "${site.ollama_url}"
        api_key  = "ollama"
    [profiles.ollama.sampling]
        think = "low"
        [profiles.ollama.sampling.options]
            temperature      = 1.0
            top_p            = 0.95
            top_k            = 20
            min_p            = 0.0
            presence_penalty = 0.0
            repeat_penalty   = 1.0
            num_predict      = 4096
            num_ctx          = 16384
```

Различается только `sampling`. Всё остальное общее: секция `[http]` одна на
приложение, и оба провайдера ходят через один транспортный компонент
(`ChatExchange` в `boba/llm/http.py`) — ретраи по 429/5xx, повтор до первого
чанка, вотчдог паузы и дампы у них одинаковые по построению, а не по
совпадению настроек. Провайдер отвечает только за wire-формат: собрать тело
и разобрать чанк.

Таблица `sampling` не проверяется и не переименовывается ни в одном из
случаев — что принимает провайдер, решает администратор профиля. Отсюда и
нужна эта диагностика: ошибиться легко, а ошибка молчит.

Единственное исключение — `stop`: его кладёт мост графа верхним уровнем
конверта, а нативный формат держит внутри `options`, поэтому провайдер
перекладывает его сам.

---

## Чек-лист диагностики

1. **Параметр в теле запроса?** Включите `dump` в секции транспорта и
   посмотрите файл. Нет — правьте конфиг: не тот профиль, не та секция,
   опечатка.
2. **Сервер не ругается?** `docker logs ollama | grep "invalid option"`.
   Ругается — исправьте имя. Молчит, но ключ лежал вне `options` — проверьте
   структуру запроса в `api/types.go`: молчание тут ничего не гарантирует.
3. **Параметр дошёл до промпта?** `_debug_render_only: true` на нативном
   `/api/chat` — глазами найдите ожидаемое изменение в тексте.
4. **Шаблон вообще про него знает?** `ollama show --modelfile` → строка
   `RENDERER` → соответствующий файл в `model/renderers/`. Для vLLM —
   `chat_template.jinja` модели на HuggingFace.
5. **Эффект измерим?** Прогоните одну задачу с двумя значениями и сравните
   `eval_count` и длину `message.thinking` — если цифры совпали, параметр не
   применился, чем бы ни клялся конфиг.
