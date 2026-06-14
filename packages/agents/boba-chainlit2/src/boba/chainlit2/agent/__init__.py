from typing import Literal

from httpx import AsyncClient
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import MessagesState
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode
from pydantic import SecretStr

from boba.chainlit2.infra.config import AppConfig

__all__ = [
    "build_agent",
    "build_langgraph",
    "get_weather",
]


@tool
def get_weather(city: Literal["nyc", "sf"]):
    """Use this to get weather information."""
    if city == "nyc":
        return "It might be cloudy in nyc"
    if city == "sf":
        return "It's always sunny in sf"
    raise AssertionError("Unknown city")


def build_langgraph(c: AppConfig, client: AsyncClient) -> CompiledStateGraph:
    """Собирает agent-граф; модель и эндпоинт берутся из AppConfig (а не из env)."""
    chat = ChatOpenAI(
        http_async_client=client,
        model=c.profile.model,
        base_url=c.profile.openai.base_url,
        api_key=SecretStr(c.profile.openai.api_key),
        temperature=c.temperature,
    )

    tools = [get_weather]
    model = chat.bind_tools(tools)
    final_model = chat.with_config(tags=["final_node"])
    tool_node = ToolNode(tools=tools)

    def should_continue(state: MessagesState) -> Literal["tools", "final"]:
        last_message = state["messages"][-1]
        # tool_calls есть только у AIMessage; есть вызов — в tools, иначе финал
        res = "tools" if getattr(last_message, "tool_calls", None) else "final"
        return res

    async def call_model(state: MessagesState):
        res = {"messages": [await model.ainvoke(state["messages"])]}
        return res

    async def call_final_model(state: MessagesState):
        last_ai_message = state["messages"][-1]
        response = await final_model.ainvoke(
            [
                SystemMessage("Rewrite this in the voice of Al Roker"),
                HumanMessage(last_ai_message.content),
            ]
        )
        # перезаписываем последнее AI-сообщение агента
        response.id = last_ai_message.id
        return {"messages": [response]}

    builder = StateGraph(MessagesState)
    builder.add_node("agent", call_model)
    builder.add_node("tools", tool_node)
    builder.add_node("final", call_final_model)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", should_continue)
    builder.add_edge("tools", "agent")
    builder.add_edge("final", END)
    return builder.compile()


def build_agent(
    c: AppConfig, client: AsyncClient, saver: BaseCheckpointSaver
) -> CompiledStateGraph:
    chat = ChatOpenAI(
        http_async_client=client,
        model=c.profile.model,
        base_url=c.profile.openai.base_url,
        api_key=SecretStr(c.profile.openai.api_key),
        temperature=c.temperature,
    )

    system_prompt = """
    Ты тот самый ассистент
    """

    agent = create_agent(
        model=chat,
        tools=[get_weather],
        system_prompt=system_prompt,
        checkpointer=saver,
    )

    return agent
