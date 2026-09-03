# Патч фронта chainlit и пин версии

Фронт chainlit собирается из исходников тега, указанного в
`packages/agents/boba-chainlit/web/chainlit-ui/UPSTREAM`, с нашими файлами из
`web/chainlit-ui/overlay/` поверх, и подключается через `custom_build = "./ui"`
в `.chainlit/config.toml`. Пакет `chainlit` в pyproject зафиксирован точно
(`chainlit==2.11.1`): overlay подменяет целые файлы upstream и имеет смысл
только для этой версии. Тест `tests/test_chainlit_ui_pin.py` сверяет UPSTREAM,
пин в pyproject и установленный пакет.

## Проблема

Фронт chainlit 2.11.1 перерисовывает страницу на каждый socket-кадр
`stream_token`:

- `useChatMessages` подписывает на атом `messagesState` страницу Chat, ScrollContainer,
  MessagesContainer и `MessageButtons` под каждым сообщением. Любой токен меняет
  атом, React перерисовывает сайдбар, композер, список тредов и кнопки всех
  сообщений треда.
- `useChatSession` на каждый токен делает `setMessages`, а утилиты дерева
  сообщений копируют путь до шага и ищут id полным обходом с lodash `isEqual`.
- ScrollContainer на каждое изменение `messages` делает `querySelectorAll` и
  читает `offsetHeight` сиблингов: forced layout на каждый токен.
- Сервер шлёт кадр на каждый токен без склейки.

На слабой машине это выглядит как «после Stop токены продолжают рисоваться»:
очередь принятых кадров дорабатывается по 5–7 мс на кадр.

## Замер

Стенд ui-тестов, фейковый LLM, 60 одинаковых ходов подряд: 200 слов thinking,
вызов инструмента, ответ на 120 слов, токен — слово. Метрики Chrome через CDP
после принудительного GC, JS-время — `ScriptDuration` за ход.

| вариант                          | кадров/ход | JS на ход, 1-й | JS на ход, 60-й | рост  | layout/ход |
|----------------------------------|-----------:|---------------:|----------------:|------:|-----------:|
| исходный 2.11.1                  |        333 |        1723 мс |         2826 мс | ×1.64 |        350 |
| патч фронта                      |        333 |         496 мс |          580 мс | ×1.17 |         80 |
| патч фронта + склейка на сервере |         34 |         267 мс |          385 мс | ×1.44 |         45 |

Heap растёт на ~0.5 MB за ход во всех вариантах: это React-инстансы
смонтированных сообщений, а не текст (строки — 3.7 MB из 62 MB снапшота).
Текст thinking и tool-результатов на стоимость не влияет: вариант
`cot = "hidden"` дал те же цифры, что и полный.

## Что правится

Overlay, файл к файлу upstream 2.11.1; каждая правка помечена `// boba:`.

| файл                                                   | правка                                                                 |
|--------------------------------------------------------|------------------------------------------------------------------------|
| `frontend/src/components/chat/Messages/Message/Buttons/index.tsx` | подписка только на `firstUserInteraction`, компонент в `memo` |
| `frontend/src/components/chat/index.tsx`               | `threadId` из `currentThreadIdState`, страница не подписана на messages |
| `frontend/src/components/chat/ScrollContainer.tsx`     | пересчёт спейсера раз в кадр отрисовки, элемент вопроса ищется при его смене |
| `libs/react-client/src/utils/message.ts`               | сравнение id через `===`, один поиск родителя на список, `applyTokens` |
| `libs/react-client/src/useChatSession.ts`              | токены копятся и применяются одним `setMessages` на кадр; другие события сбрасывают пачку |

Сервер: `StreamBatch` в `boba/chainlit/rendering/chat_view.py` копит токены
ответа и рассуждений и отдаёт `stream_token` раз в 50 мс; любой другой кадр
ленты сначала выталкивает пачку, порядок кадров сохраняется.

## Сборка

- `make fetch` кладёт в `build/chainlit/src` тарбол исходников тега и pnpm-store
  зависимостей фронта (`chainlit-ui-store-<версия>.tar.gz`); сборка потом идёт
  без сети.
- Стадия Dockerfile `chainlit-ui-build` и цель `make chainlit-ui` зовут один
  скрипт `build/chainlit/scripts/chainlit_ui.sh`: распаковать, наложить overlay,
  `pnpm install --offline`, `type-check`, собрать `react-client` и `frontend`.
- Образ: dist в `${BOBA_DIR}/app_root/ui`; dev-дерево: `compose/chainlit/app_root/ui`,
  `make dev` собирает сам. Без overlay сборка даёт бандл, байт-в-байт равный
  wheel, так проверяется сам конвейер.
- Без `app_root/ui` chainlit молча берёт dist из wheel: проверять, что бандл
  на странице не `index-LFVFt-Wr.js`.

## SLA

`tests/ui/test_feed_perf_ui.py`: шесть длинных ходов на коротком треде, сорок
коротких ходов разогрева, снова шесть длинных. Главный порог — рост стоимости
длинного хода с длиной ленты, ×1.25: он не зависит от загрузки хоста. Вендорный
фронт даёт ×1.51 и падает, патченный ×1.09. Дополнительно: кадров в ходе,
JS-время на кадр, heap за ход и число кадров после Stop (сейчас 9 при пороге 40).

## Обновление chainlit

1. Поменять версию в `UPSTREAM` и пин в pyproject, `uv lock`, `make fetch`.
2. Каждый файл overlay сверить с новым upstream: правки переносятся руками,
   если upstream сам не убрал подписку на `messagesState`.
3. `make chainlit-ui`, прогнать `tests/ui`, включая `test_feed_perf_ui.py`.
