# boba-ext-files

Builtin file-system tools для агента Boba: `cat`, `ls`, `grep`, `edit`,
`write`, `append`, `mv`, `cp`, `rm`, `mkdir`, `touch`, `stat`, `tree`,
`pwd`, `cd`. Все работают через
`ToolContext.project_workspace`, поэтому привязаны к
session-workspace'у пользователя — никакого свободного доступа к
хосту.

## Установка

In-tree dev-режим (editable):

```bash
pip install -e ./extensions/files
```

Из готового wheel — обычным `pip install boba-ext-files`.

После установки `ToolPluginLoader` подхватит entry-point
`boba.tools/files` автоматически — никакой ручной регистрации не
требуется.

## Конфиг

Пакет не имеет своего конфига и не читает
`AppConfig.extensions["files"]` — все параметры (например, лимит строк
для `cat`) захардкожены в Tool-классах.
