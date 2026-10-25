"""Перечень ключей site.toml, на которые ссылается общий конфиг приложения.

Вызов:
  site_keys.py <каталог conf пакета приложения>

Общий конфиг и файлы плагинов называют значения машины ссылками ${env.<ключ>} и
${site.<ключ>}; сами значения лежат в site.toml случая запуска. Скрипт читает ссылки
и печатает ключи по секциям с местом первого использования: по этому перечню пишут
первый site.toml. Источник перечня — сам конфиг, поэтому устареть он не может.
"""

import importlib
import re
import sys
import tomllib
from pathlib import Path

REFERENCE = re.compile(r"\$\{(env|site)\.([A-Za-z0-9_.-]+?)(?=\}|\.\$\{)")
COMPUTED = {"base": "вычисляется: каталог над conf/, в котором лежит site.toml"}
# ключи [env], которые переопределяет переменная окружения BOBA_<ИМЯ>
OVERRIDABLE = [
    "base",
    "data",
    "port",
    "instance_id",
    "host",
    "url_prefix",
    "cgroup_base",
    "app_root",
    "messaging_provider",
    "tool_launcher",
    "mcp_host",
    "mcp_port",
    "mcp_scheme",
    "mcp_prefix",
    "public_url",
    "agent_mode",
    "agent_command",
    "agent_host",
    "agent_port",
    "agent_scheme",
    "agent_prefix",
]
OVERRIDE_NAMES = {"messaging_provider": "MESSAGING"}
# ключи [env], которые код читает напрямую, без ссылки из конфига: пути песочницы
CODE_KEYS = {
    "cgroup_base": "читает лаунчер инструментов: каталог cgroup песочницы",
}

# способ входа подключения: ключ <x>_auth_method выбирает таблицу [site.<x>_auth.<имя>];
# варианты её поля method читаются из модели, которая таблицу проверяет
AUTH_METHOD_SUFFIX = "_method"
AUTH_MODELS = {
    "pg_auth": ("boba.db.postgres.connection", "PostgresConfig", "auth"),
    "pg_history_auth": ("boba.db.postgres.connection", "PostgresConfig", "auth"),
    "llm_auth": ("boba.transport.http.connection", "HttpConnection", "auth"),
    "confluence_auth": ("boba.transport.http.connection", "HttpConnection", "auth"),
    "ldap_auth": ("boba.auth.config", "LdapRolesConfig", "auth"),
    "mail_auth": ("boba.tool.mail.tools", "SmtpConfig", "auth"),
}
# входы, которые приложение собирает само: учётка пользователя и билет вызова
APP_ONLY_METHODS = {"kerberos_delegated", "kerberos_ticket"}


def leaves(node: object, prefix: str) -> list[tuple[str, str]]:
    if isinstance(node, dict):
        found: list[tuple[str, str]] = []
        for key, value in node.items():
            found.extend(leaves(value, f"{prefix}{key}."))
        return found
    if isinstance(node, list):
        found = []
        for item in node:
            found.extend(leaves(item, prefix))
        return found
    if isinstance(node, str):
        return [(prefix.rstrip("."), node)]
    return []


def collect(conf: Path) -> dict[str, dict[str, str]]:
    used: dict[str, dict[str, str]] = {"env": {}, "site": {}}
    files = [(conf / "config.toml", "")]
    for path in sorted((conf / "plugins").glob("*.toml")):
        files.append((path, f"tool.{path.stem}."))
    for path, prefix in files:
        with path.open("rb") as body:
            document = tomllib.load(body)
        for field, value in leaves(document, prefix):
            for section, key in REFERENCE.findall(value):
                used[section].setdefault(key, field)
    return used


def auth_methods(table: str) -> list[tuple[str, list[str]]]:
    """Способы таблицы входа: значение method и обязательные поля варианта."""
    if table not in AUTH_MODELS:
        raise SystemExit(
            f"site_keys: no model for the login tables [site.{table}.*]; "
            "add the model that validates them to AUTH_MODELS"
        )

    module, name, field = AUTH_MODELS[table]
    model = getattr(importlib.import_module(module), name)
    schema = model.model_json_schema()
    definitions = schema["$defs"]
    found: list[tuple[str, list[str]]] = []
    for ref in schema["properties"][field]["discriminator"]["mapping"].values():
        variant = definitions[ref.rsplit("/", 1)[-1]]
        method = variant["properties"]["method"]["const"]
        if method in APP_ONLY_METHODS:
            continue
        fields = [key for key in variant.get("required", []) if key != "method"]
        found.append((method, fields))
    return found


def print_auth(used: dict[str, str]) -> None:
    tables = sorted(
        key[: -len(AUTH_METHOD_SUFFIX)]
        for key in used
        if key.endswith(f"_auth{AUTH_METHOD_SUFFIX}")
    )
    if not tables:
        return
    print(
        "\n# способы входа: <ключ>_method называет таблицу [site.<ключ>.<имя>],"
        " способ задаёт её поле method"
    )
    for table in tables:
        print(f"\n[site.{table}.<имя>]  — {used[table]}")
        for method, fields in auth_methods(table):
            print(f'    method = "{method}"  — {", ".join(fields) or "без полей"}')


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2

    conf = Path(argv[1])
    used = collect(conf)
    if "sandbox" in used["env"]:
        for key, note in CODE_KEYS.items():
            used["env"].setdefault(key, note)
    print(f"# ключи site.toml для {conf}")
    print("# каждая строка: ключ — где общий конфиг использует его впервые")
    for section in ("env", "site"):
        print(f"\n[{section}]")
        for key in sorted(used[section]):
            note = used[section][key]
            if section == "env" and key in COMPUTED:
                print(f"    # {key}: {COMPUTED[key]}")
                continue
            hint = ""
            if section == "env" and key in OVERRIDABLE:
                name = OVERRIDE_NAMES.get(key, key.upper())
                hint = f"; переопределяется переменной BOBA_{name}"
            print(f"    {key}  — {note}{hint}")
    print(
        "\n# ключ вида a.b — таблица [site.a] с полем b; ключ, оканчивающийся"
        " другой ссылкой, выбирается её значением"
    )
    print_auth(used["site"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
