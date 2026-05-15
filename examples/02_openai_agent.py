"""
02 — Real LangChain agent + OpenAI, audited by AEVS.

The model decides which tools to call; AEVS records every one of them.
This example resolves each receipt per-call via ``tool_call_id`` (compare
example 03, which uses the bulk ``aevs.get_reference_ids()``).

Run it
------
    pip install 'aevs[langchain]' langchain langchain-openai python-dotenv

Then put your keys in ``examples/.env`` (copy ``examples/.env.example``):

    OPENAI_API_KEY=sk-...
    AEVS_API_KEY=aevs_sk_<key_id>_<hex_secret>   # https://aevs.fetch.ai
    AEVS_AGENT_ID=<your-agent-uuid>

    python examples/02_openai_agent.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

import aevs

load_dotenv(Path(__file__).parent / ".env")
logging.basicConfig(level=logging.WARNING, format="%(message)s")


@tool
def multiply(a: float, b: float) -> float:
    """Multiply two numbers and return the product."""
    return a * b


@tool
def get_weather(city: str) -> str:
    """Return today's weather for *city* (mocked for the demo)."""
    fake = {
        "tokyo": "21°C, light rain",
        "paris": "17°C, partly cloudy",
        "san francisco": "15°C, foggy",
    }
    return fake.get(city.lower(), f"No weather data for {city!r}")


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(
            f"error: ${name} is not set.\n"
            f"  Set it in your shell: export {name}=...\n"
            f"  See examples/02_openai_agent.py docstring for setup details."
        )
    return value


def main() -> None:
    _require_env("OPENAI_API_KEY")
    _require_env("AEVS_API_KEY")
    _require_env("AEVS_AGENT_ID")

    # AEVS_API_KEY and AEVS_AGENT_ID are read from env automatically.
    aevs.configure(buffer_path="./buffer.db", receipt_visibility="public")
    aevs.enable(frameworks=["langchain"])

    print(f"\n=== AEVS session: {aevs.get_session_id()} ===")

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    agent = create_agent(llm, tools=[multiply, get_weather])

    question = "What is 7 times 6, and what's the weather in Tokyo today?"
    print(f"\n--- Asking the agent ---\n{question}")

    response = agent.invoke({"messages": [("user", question)]})
    final = response["messages"][-1].content
    print(f"\n--- Agent answer ---\n{final}")

    print("\n--- Tool calls AEVS intercepted (looked up via tool_call_id) ---")
    tool_messages = [m for m in response["messages"] if isinstance(m, ToolMessage)]
    if not tool_messages:
        print("  (none — the model answered without calling any tools)")
    for msg in tool_messages:
        ref_id = aevs.get_reference_id(msg.tool_call_id)
        print(
            f"  tool={msg.name:<12}  "
            f"tool_call_id={msg.tool_call_id}  "
            f"reference_id={ref_id}"
        )

    aevs.clear_reference_ids()
    aevs.flush()
    aevs.disable()

    print("\nSearch for any reference_id above on https://explorer.aevs.fetch.ai to view the receipt in a UI.")


if __name__ == "__main__":
    main()
