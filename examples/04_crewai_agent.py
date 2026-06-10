"""
04 — A CrewAI agent that takes a *consequential* action, audited by AEVS.

The first three examples answer questions (math, weather). This one does
something you'd actually want a paper trail for: a support agent decides
whether to **issue a refund** and, if so, calls a tool that moves money.

That's exactly where AEVS earns its keep. When an autonomous agent can
spend money, change records, or act on a user's behalf, "the LLM said it
did" isn't good enough for finance, compliance, or a dispute. AEVS turns
every tool call into a signed, hash-chained receipt you can hand to an
auditor — without changing your tools or your crew. You just enable it.

The agent picks the tool and the arguments; AEVS records the *real*
inputs and outputs that executed.

Run it
------
    pip install 'aevs[crewai]' crewai python-dotenv

Then put your keys in ``examples/.env`` (copy ``examples/.env.example``):

    ASI_API_KEY=sk_...                             # https://asi1.ai
    AEVS_API_KEY=aevs_sk_<key_id>_<hex_secret>     # https://aevs.fetch.ai
    AEVS_AGENT_ID=<your-agent-uuid>

    python examples/04_crewai_agent.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from crewai import LLM, Agent, Crew, Task
from crewai.tools import tool
from dotenv import load_dotenv

import aevs

load_dotenv(Path(__file__).parent / ".env")
logging.basicConfig(level=logging.WARNING, format="%(message)s")

ASI_BASE_URL = "https://api.asi1.ai/v1"
ASI_MODEL = "asi1-mini"

# A tiny mock "ledger" so the demo is self-contained. In real life this
# tool would hit your payments provider — the kind of side effect you
# really want a verifiable receipt for.
_PAID_ORDERS = {"A-1001": 49.99, "A-1002": 12.50}


@tool("issue_refund")
def issue_refund(order_id: str, amount_usd: float, reason: str) -> str:
    """Refund a customer for a paid order. Use only for orders that exist.

    Args:
        order_id: The order to refund, e.g. "A-1001".
        amount_usd: Amount to refund in USD; cannot exceed what was paid.
        reason: Short human-readable reason for the refund.
    """
    paid = _PAID_ORDERS.get(order_id)
    if paid is None:
        raise ValueError(f"Unknown order {order_id!r} — refusing to refund.")
    refund = min(amount_usd, paid)  # never refund more than was actually paid
    return f"Refunded ${refund:.2f} for {order_id} ({reason}). Confirmation: RF-{order_id}"


# A refund is a one-shot action: once it runs, its confirmation *is* the
# answer. Setting result_as_answer makes CrewAI return that output directly
# and stop, instead of looping the agent — which weaker models tend to do,
# re-calling the tool several times. (If you remove this, AEVS will happily
# record every one of those repeat calls — which is exactly the point of an
# audit trail: you'd want to know your agent issued the refund 7 times.)
issue_refund.result_as_answer = True


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(
            f"error: ${name} is not set.\n"
            f"  Set it in your shell: export {name}=...\n"
            f"  See examples/04_crewai_agent.py docstring for setup details."
        )
    return value


def main() -> None:
    asi_key = _require_env("ASI_API_KEY")
    _require_env("AEVS_API_KEY")
    _require_env("AEVS_AGENT_ID")

    # AEVS_API_KEY and AEVS_AGENT_ID are read from env automatically.
    aevs.configure(buffer_path="./buffer.db", receipt_visibility="public")
    aevs.enable(frameworks=["crewai"])

    print(f"\n=== AEVS session: {aevs.get_session_id()} ===")
    print(f"=== Model: {ASI_MODEL} via {ASI_BASE_URL} ===")

    # CrewAI routes LLM calls through litellm; the "openai/" prefix points
    # it at any OpenAI-compatible endpoint (here, Fetch.ai's ASI:One).
    llm = LLM(
        model=f"{ASI_MODEL}",
        base_url=ASI_BASE_URL,
        api_key=asi_key,
        temperature=0,
    )

    support_agent = Agent(
        role="Customer Support Refund Agent",
        goal="Resolve refund requests fairly using the issue_refund tool.",
        backstory=(
            "You handle refund requests for an online store. You only refund "
            "orders that exist, never more than was paid, and you always call "
            "the issue_refund tool to actually process an approved refund."
        ),
        tools=[issue_refund],
        llm=llm,
        verbose=False,
    )

    request = "Customer says their coffee grinder (order A-1001) arrived broken. Refund them in full."
    task = Task(
        description=request,
        agent=support_agent,
        expected_output="A one-line confirmation of the refund, including the confirmation code.",
    )

    print(f"\n--- Refund request ---\n{request}")

    crew = Crew(agents=[support_agent], tasks=[task])
    result = crew.kickoff()
    print(f"\n--- Agent outcome ---\n{result}")

    # Every tool call in a single kickoff() shares one invocation_id, so the
    # whole agent run is one auditable group of receipts.
    print("\n--- Tool calls AEVS intercepted (signed receipts) ---")
    refs = aevs.get_reference_ids(clear=True)
    if not refs:
        print("  (none — the agent answered without calling any tools)")
    for entry in refs:
        print(f"  seq={entry['seq']}  tool={entry['tool_name']:<14}  reference_id= {entry['reference_id']}")

    aevs.flush()
    aevs.disable()

    if refs:
        ref = refs[0]["reference_id"]
        print(f"\nVerify this refund receipt — no auth required:\n  https://api.aevs.fetch.ai/v1/receipts/verify/{ref}")
    print("\nOr search any reference_id above in https://explorer.aevs.fetch.ai to view the receipt in a UI.")


if __name__ == "__main__":
    main()
