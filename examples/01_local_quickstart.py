"""
01 — Local quickstart.

The minimal AEVS flow: configure, enable, call a tool, see that AEVS
captured it (and shipped a signed receipt to the backend). Open the
explorer URL it prints to verify the receipt in your browser.

Get your credentials
--------------------
1. Visit https://aevs.fetch.ai and sign in.
2. Create an API key — copy the full ``aevs_sk_<key_id>_<hex_secret>`` string.
3. Create an agent in the dashboard — copy its UUID (the ``agent_id``).
4. Drop both into ``examples/.env`` (copy ``.env.example``).

Run it
------
    pip install 'aevs[langchain]' python-dotenv
    python examples/01_local_quickstart.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.tools import tool

import aevs

load_dotenv(Path(__file__).parent / ".env")


@tool
def add(a: int, b: int) -> int:
    """Return the sum of two integers."""
    return a + b


def main() -> None:
    if not os.environ.get("AEVS_API_KEY") or not os.environ.get("AEVS_AGENT_ID"):
        sys.exit(
            "error: AEVS_API_KEY and AEVS_AGENT_ID must be set.\n"
            "  Get them at https://aevs.fetch.ai (sign in -> create key, create agent),\n"
            "  then put them in examples/.env (see examples/.env.example)."
        )

    aevs.configure()  # reads AEVS_API_KEY and AEVS_AGENT_ID from the env
    aevs.enable(frameworks=["langchain"])

    result = add.invoke({"a": 6, "b": 7})
    print(f"add(6, 7) = {result}")

    for entry in aevs.get_reference_ids(clear=True):
        print(f"  AEVS captured: seq={entry['seq']}  tool={entry['tool_name']}  reference_id= {entry['reference_id']}")


    aevs.flush()
    aevs.disable()
    print(f"\nAdditionaly, you can verify the receipt via API : https://api.aevs.fetch.ai/v1/receipts/verify/{entry['reference_id']}")
    print("\nSearch for the reference_id above in https://explorer.aevs.fetch.ai to view the receipt in a UI.")


if __name__ == "__main__":
    main()
