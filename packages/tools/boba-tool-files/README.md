# boba-ext-files

Builtin file-system tools для агента Boba: `cat`, `read_bytes`, `ls`,
`grep`, `edit`, `write`, `append`, `mv`, `cp`, `rm`, `mkdir`, `touch`,
`stat`, `tree`, `pwd`, `cd`, `unzip`. Все работают через
`ToolContext.project_workspace`, поэтому привязаны к
session-workspace'у пользователя — никакого свободного доступа к
хосту.

## Установка

In-tree dev-режим (editable):

```bash
pip install -e ./packages/boba-ext-files
```

Из готового wheel — обычным `pip install boba-ext-files`.

После установки `ToolPluginLoader` подхватит entry-point
`boba.tools/files` автоматически — никакой ручной регистрации не
требуется.

## Конфиг

Расширение подключается явно — секцией `[ext.files]`:

```toml
[ext.files]
enable = true
# tools_allow = ["cat", "ls", "grep"]   # пусто = все tools пакета
```

Без `enable = true` (или без секции вовсе) — tools пакета не
регистрируются. `tools_allow` опционален: пустой список — все tools,
заполненный — whitelist по именам tools (`cat`, `read_bytes`, `ls`,
`grep`, `pwd`, `cd`, `edit`, `write`, `append`, `mv`, `cp`, `rm`,
`mkdir`, `touch`, `stat`, `tree`, `unzip`).
