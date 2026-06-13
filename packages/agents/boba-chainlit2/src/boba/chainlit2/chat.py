from typing import Annotated

import chainlit as cl
from openai import AsyncOpenAI

from boba.chainlit2.infra.di import Depend, inject
from boba.chainlit2.infra.providers import client_settings, debug_client


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
@inject
async def new_message(
    message: cl.Message,
    client: Annotated[AsyncOpenAI, Depend(debug_client)],
    settings: Annotated[dict, Depend(client_settings)],
):
    template = """SQL tables (and columns):
* Customers(customer_id, signup_date)
* Streaming(customer_id, video_id, watch_date, watch_minutes)

A well-written SQL query that {input}:
```"""

    messages: list = [
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
