"""
03 — Same agent, swapped to Fetch.ai's ASI:One via the OpenAI-compatible API.

AEVS is provider-agnostic: swap the model, keep the audit trail. Only the
``ChatOpenAI`` constructor changes vs example 02.

ASI:One reference: https://docs.asi1.ai

Run it
------
    pip install 'aevs[langchain]' langchain langchain-openai python-dotenv

Then put your keys in ``examples/.env`` (copy ``examples/.env.example``):

    ASI_API_KEY=sk_...                             # https://asi1.ai
    AEVS_API_KEY=aevs_sk_<key_id>_<hex_secret>     # https://aevs.fetch.ai
    AEVS_AGENT_ID=<your-agent-uuid>

    python examples/03_asi_agent.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

import aevs

load_dotenv(Path(__file__).parent / ".env")
logging.basicConfig(level=logging.WARNING, format="%(message)s")

ASI_BASE_URL = "https://api.asi1.ai/v1"
ASI_MODEL = "asi1-mini"


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
            f"  See examples/03_asi_agent.py docstring for setup details."
        )
    return value


def main() -> None:
    asi_key = _require_env("ASI_API_KEY")
    _require_env("AEVS_API_KEY")
    _require_env("AEVS_AGENT_ID")

    aevs.configure(buffer_path="./buffer.db", receipt_visibility="proof_only")
    aevs.enable(frameworks=["langchain"])

    print(f"\n=== AEVS session: {aevs.get_session_id()} ===")
    print(f"=== Model: {ASI_MODEL} via {ASI_BASE_URL} ===")

    llm = ChatOpenAI(
        model=ASI_MODEL,
        base_url=ASI_BASE_URL,
        api_key=asi_key,
        temperature=0,
    )
    agent = create_agent(llm, tools=[multiply, get_weather])

    question = "What is 7 times 6, and what's the weather in Tokyo today?"
    print(f"\n--- Asking the agent ---\n{question}")

    response = agent.invoke({"messages": [("user", question)]})
    final = response["messages"][-1].content
    print(f"\n--- Agent answer ---\n{final}")

    print("\n--- Tool calls AEVS intercepted ---")
    refs = aevs.get_reference_ids(clear=True)
    if not refs:
        print("  (none — the model answered without calling any tools)")
    for entry in refs:
        print(f"  seq={entry['seq']}  tool={entry['tool_name']:<12}  reference_id= {entry['reference_id']}")

    aevs.flush()
    aevs.disable()

    print("\nSearch for the reference_id above in https://explorer.aevs.fetch.ai to view the receipt in a UI.")


if __name__ == "__main__":
    main()
