# Правила под отчёт ИБ

Пак повторяет категории сканера PT Application Inspector теми средствами,
которые есть в semgrep OSS. Запускается через `make -C build sec SEC_MODE=full` вместе со
скачанными паками (`python`, `secrets`, `dockerfile`, `javascript`, `react`)
по всему дереву, а не только по `packages`.

| Правило | Категория отчёта |
|---|---|
| `boba-file-op-dynamic-path` | Arbitrary File/Directory Creation, Deletion, Modification, Reading |
| `boba-subprocess-dynamic-argv` | OS Command Injection |
| `boba-browser-api-direct` | Использование браузерного api (в режиме SSR) |
| `boba-plaintext-url` | Missing Encryption of Sensitive Data |
| `boba-hardcoded-secret-literal` | Use of Hard-coded Password |
| `boba-untrusted-install` | Установка кода из недоверенных источников |
| `boba-apt-get-update-alone` | Использование одиночной инструкции apt-get update |
| `boba-log-forging` | Log Forging |
| `boba-raw-fd-write` | Uncontrolled Data Manipulation |

Разница с оригиналом: PT ведёт поток данных от точек входа через файлы, а
semgrep OSS межфайловый taint не умеет. Поэтому правила ловят сам факт
динамического аргумента у опасного вызова, без анализа источника — находок
выходит больше, чем в отчёте, и часть из них заведомо своя.

## Правила намеренно тупые

Исключений «этот вызов мы считаем безопасным» здесь нет и быть не должно.
Правило видит только форму: `open(переменная)` против `open("литерал")`, и
отличить проверенный путь от непроверенного оно не может — для этого нужен
анализ потока данных, которого в OSS-semgrep нет.

Поэтому число находок в `SEC_MODE=full` не сходится к нулю и не является воротами:
приложение, которое ведёт журналы, монтирует образы и управляет cgroup, строит
пути и команды из переменных по своей природе. Это список мест для присмотра.
Ворота — `make -C build sec` без режима (или `bash sec/sec.sh all`): там ruff,
bandit, semgrep-паки и pip-audit. Успешный код SCA не означает, что проверены
локальные версии: пропуски пакетов с суффиксом `+boba.*` учитывать отдельно и сверять
по версии upstream и сопровождаемому patch. `sec.sh sca` и `all` дополнительно
проверяют upstream-версии всех таких пакетов из lock, включая Chainlit,
LiteralAI и python-oracledb. Этот отдельный
прогон ищет известные CVE исходной версии; сам патч требует проверки изменений,
а wheel — проверки происхождения и сборки.

SCA экспортирует все workspace-пакеты и extras: среди дополнительных наборов
есть production-провайдеры OCR, чтения документов и ONNX. Так проверяются
также инструменты разработки из lock; выключение extras оставляло бы часть
фактической поставки вне аудита.

Чтобы сохранить исходные JSON-отчёты для разбора отдельных находок:

```bash
SEC_REPORT_DIR=/tmp/boba-security make -C build sec SEC_MODE=full
```

Каталог содержит `semgrep.json` и `bandit.json`; новые файлы доступны только
владельцу. Без `SEC_REPORT_DIR` временные отчёты удаляются после сводки.
Ошибка чтения отчёта возвращает ненулевой код, как и ошибка сканера.
