import os
import re
import socket
from pathlib import Path

import chainlit as cl
import httpx
from chainlit.context import ChainlitContextException, get_context
from chainlit.server import sio
from engineio.payload import Payload
from log import DumpingTransport, logger_bootstrap
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from openai.types.chat import ChatCompletionMessageParam

# bootstrap logger'а
logger_bootstrap()


def chainlit_filename(request: httpx.Request) -> str:
    """Метка запроса из контекста chainlit: пользователь + id сообщения."""
    try:
        ctx = get_context()
    except ChainlitContextException:
        return "no-context"

    who = "anon"
    if user := getattr(ctx.session, "user", None):
        who = user.identifier

    # parent_id у run-step'а on_message — это id входящего сообщения;
    # вне сообщения (например, on_chat_start) остаётся id сессии
    what = ctx.session.id
    if run := ctx.current_run:
        what = run.parent_id or run.id

    label = re.sub(r"[^\w.@-]", "_", f"{who}-{what}")

    res = f"{label}-{request.url.host}.log"

    return res


# engine.io heartbeat: даём клиенту пережить долгие паузы на брейкпоинтах
sio.eio.ping_interval = 300
sio.eio.ping_timeout = 300
# за паузу на брейкпоинте клиент копит события и шлёт их одним
# polling-POST; дефолтных 16 пакетов не хватает
Payload.max_decode_packets = 256

cl.instrument_openai()

timeout = httpx.Timeout(
    connect=5,      # установка TCP-соединения с хостом (включая TLS handshake)
    read=100,       # ожидание данных от сервера; при stream=True — пауза между чанками
    write=100,      # отправка тела запроса на сервер
    pool=5,         # ожидание свободного соединения из пула httpx (когда все заняты)
)
limits = httpx.Limits(
    max_connections=50,
    max_keepalive_connections=10,
    keepalive_expiry=5,
)
proxy = None
verify = httpx.create_ssl_context(verify=True, cert=None, trust_env=True)
# канал, по которому транспорт сообщает socket-уровню текущий файл дампа
transport = DumpingTransport(
    # имя файла дампа
    dump_dir=Path("dumps"),
    dump_file=chainlit_filename,
    http2=False,
    verify=verify,
    limits=limits,
    proxy=proxy,
    retries=3,
    socket_options=[
        # SO_KEEPALIVE = 1 - включить TCP keepalive
        # SO_KEEPALIVE = 0 - не использовать TCP keepalive
        (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        # файрволы выкидывают соединения, которые не пересылают пакеты по установленному
        # соединению обычно файрволны использовают параметры в район 5-10 минут и здесь
        # мы защищяемся от произвольного выкидывания нашего соединения третьей стороной
        # 1. если соединение простаивает TCP_KEEPIDLE секунд ядро ОС будет слать пробу
        # 2. если удаленная сторона жива, ее ядро (НЕ программа, а именно linux)
        #    отвечает ASK
        # 3. если ответ нет значит ядро будет повторять еще TCP_KEEPCNT раз через каждые
        #    TCP_KEEPINTVL
        # Соединение объявляется мертвым и при следующей попытке чтения/записи сокета
        # будет получена ошибка ECONNABORTED/ETIMEDOUT
        (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60),
        (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10),
        (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 10),
        # Если включить SO_KEEPALIVE и не указать остальные параметры, то они будут взят
        # из sysctl и обычно по умолчанию это /proc/sys/net/ipv4/tcp_keepalive_time
        # = 7200 (2 часа простоя!)
    ],
)

client = AsyncOpenAI(
    base_url=os.environ["OPENROUTER_BASE_URL"],
    api_key=os.environ["OPENROUTER_API_KEY"],
    http_client=DefaultAsyncHttpxClient(
        timeout=timeout,
        transport=transport,
    ),
)

template = """SQL tables (and columns):
* Customers(customer_id, signup_date)
* Streaming(customer_id, video_id, watch_date, watch_minutes)

A well-written SQL query that {input}:
```"""


settings = {
    "model": "qwen/qwen3.6-27b",
    "temperature": 0,
    "max_tokens": 500,
    "top_p": 1,
    "frequency_penalty": 0,
    "presence_penalty": 0,
    "stop": ["```"],
}


@cl.set_starters
async def starters(user: cl.User | None):
    return [
        cl.Starter(
            label=">50 minutes watched",
            message=(
                "Compute the number of customers who watched more than "
                "50 minutes of video this month."
            ),
        )
    ]


# при открытии нового чата
@cl.on_chat_start
async def on_chat_start():
    cl.user_session.set(
        "message_history",
        [
            {
                "role": "system",
                "content": (
                    "Characters from the silicon valley tv show are acting. "
                    "Gilfoyle (sarcastic) wants to push to production. Dinesh (scared)"
                    "wants to write more tests. Richard asks the question."
                ),
            }
        ],
    )


@cl.on_message
async def new_message(message: cl.Message):
    messages: list[ChatCompletionMessageParam] = [
        {
            "role": "user",
            "content": template.format(input=message.content),
        }
    ]
    stream = await client.chat.completions.create(
        messages=messages, stream=True, **settings
    )

    msg = await cl.Message(content="").send()

    async for part in stream:
        if token := part.choices[0].delta.content or "":
            await msg.stream_token(token)

    await msg.update()
