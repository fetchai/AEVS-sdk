# AEVS SDK examples

Three runnable scripts that walk you through the full AEVS flow at three
levels of realism. Each one is self-contained — pick the one that matches
what you're trying to learn.

| # | File | What it teaches | Requires |
|---|------|-----------------|----------|
| 1 | [`01_local_quickstart.py`](01_local_quickstart.py) | The minimal SDK loop — configure → enable → invoke → see what AEVS captured (and shipped a signed receipt for). No LLM. | `AEVS_API_KEY`, `AEVS_AGENT_ID` (get them at [aevs.fetch.ai](https://aevs.fetch.ai)) |
| 2 | [`02_openai_agent.py`](02_openai_agent.py) | A real LangChain agent with OpenAI picks tools to answer a multi-part query; AEVS records every tool call without changes to the agent or tools. | `OPENAI_API_KEY`, `AEVS_API_KEY`, `AEVS_AGENT_ID` |
| 3 | [`03_asi_agent.py`](03_asi_agent.py) | Same agent, swapped to Fetch.ai's ASI:One model via its OpenAI-compatible API — the only diff is three lines in the `ChatOpenAI` constructor. Demonstrates that AEVS is provider-agnostic. | `ASI_API_KEY` (from [asi1.ai](https://asi1.ai)), `AEVS_API_KEY`, `AEVS_AGENT_ID` |

## Recommended path

1. Get free credentials at [aevs.fetch.ai](https://aevs.fetch.ai) — sign
   in, create an API key, create an agent in the dashboard, copy its UUID.
2. Run **`01_local_quickstart.py`**. Read its output side-by-side with
   the source — the prints map 1:1 to the SDK steps.
3. Run **`02_openai_agent.py`** with an `OPENAI_API_KEY`. Now the model
   is choosing tools and AEVS records every choice.
4. Run **`03_asi_agent.py`** with an `ASI_API_KEY` instead. Diff it
   against `02_openai_agent.py` — exactly four lines change. The audit
   layer doesn't.

## Setup

Install the LangChain extra plus `python-dotenv` (used to read `.env`):

```bash
pip install 'aevs[langchain]' python-dotenv
```

Examples 2 and 3 additionally need a LangChain agent runtime:

```bash
pip install langchain langchain-openai
```

Copy the env template and fill in your keys:

```bash
cp examples/.env.example examples/.env
# then edit examples/.env in your editor
```

The `.env` lives next to the example scripts and is auto-loaded by
`python-dotenv` when each example starts.

Then run any of:

```bash
python examples/01_local_quickstart.py
python examples/02_openai_agent.py
python examples/03_asi_agent.py
```

## Verifying a receipt

Every example prints, for each intercepted tool call, the receipt's
`reference_id`. Verify any of them via the public backend endpoint:

```
GET https://api.aevs.fetch.ai/v1/receipts/verify/<reference_id>
```

`curl` it (or paste it into a browser) and you get the canonical JSON
proof — including the hash chain links, signed inputs/outputs hash,
the resolved internal `receipt_id`, and a `verified: true` flag.
**No auth required.** That's the audit-trail handoff: your app holds
a short opaque `reference_id`; auditors, users, and regulators get
cryptographic proof of what your agent did.

For a UI view, paste the same `reference_id` into the explorer's
search bar:

> https://explorer.aevs.fetch.ai
