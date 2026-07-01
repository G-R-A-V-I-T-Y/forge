"""One-off manual verification script — NOT part of the pytest suite.

Round 2 of live verification, requested after the captain reviewed the
first round's results (synthetic minimal test prompt) and wanted the real
production-shaped decision prompt used instead — the exact prompt an
agent actually sees on a real wake cycle, built via the real code path:

  agents/persona.py's build_system_prompt()
  agents/prompt_builder.py's build_decision_prompt()

...fed with a real agent's thesis text (silver_basin, a real trading
agent — not jade_hawk, which never trades), a fresh heartbeat snapshot
generated for real against the live Hyperliquid provider (the committed
data/heartbeat.json in the primary checkout was ~2.5h stale, well past
the 2x heartbeat_interval_seconds staleness window decision_loop.py
enforces, so a fresh one is generated here the same way forge.py's
run_heartbeat_cycle() does), and real portfolio/performance data read
from data/forge.db.

Calls all 6 remote opencode-routed tiers for real (no mocking) with this
real prompt, via the same trading-responder custom agent and UTF-8
encoding fix as scripts/verify_model_chain_live.py. Run manually:

    python scripts/verify_model_chain_live_prod_prompt.py

Results are pasted into the PR description (labeled separately from the
round-1 synthetic-prompt results) and design doc.
"""
import asyncio
import json
import time
from pathlib import Path

import yaml

from agents.persona import build_system_prompt
from agents.prompt_builder import build_decision_prompt
from market import heartbeat as heartbeat_mod
from market.provider import MarketProvider
from store.db import get_connection
from llm.model_chain import CHAIN, _run_opencode_tier

AGENT_ID = "silver_basin"  # real trading agent (funding dislocation) — not jade_hawk, which never trades
CONFIG_PATH = Path("config.yaml")
DB_PATH = Path("data/forge.db")


async def build_real_prompt() -> tuple[str, str]:
    config = yaml.safe_load(CONFIG_PATH.read_text())
    desk_config = config["desk"]

    thesis_path = Path("agents/theses") / f"{AGENT_ID}_v1.md"
    thesis_text = thesis_path.read_text(encoding="utf-8")

    conn = get_connection(str(DB_PATH))

    provider = MarketProvider(config)
    async with provider:
        # Generate a fresh heartbeat for real against the live Hyperliquid
        # provider — the same call forge.py's run_heartbeat_cycle() makes
        # on startup and on its recurring schedule. The committed
        # data/heartbeat.json in the primary checkout was stale (~2.5h
        # old), which decision_loop.py would treat as unusable.
        packet = await heartbeat_mod.generate_heartbeat(provider, config)

        system_prompt = build_system_prompt(AGENT_ID, config)
        decision_prompt = await build_decision_prompt(
            AGENT_ID, thesis_text, packet, conn, provider,
            starting_balance=desk_config["starting_balance"],
            universe=config["universe"],
        )

    conn.close()
    return system_prompt, decision_prompt


def main() -> None:
    system_prompt, decision_prompt = asyncio.run(build_real_prompt())
    message = f"{system_prompt}\n\n{decision_prompt}"

    print(f"=== Real production decision prompt for {AGENT_ID} ({len(message)} chars) ===")
    print(message[:2000])
    print("... [truncated] ...\n")

    results = []
    for tier in CHAIN:
        if tier.kind != "opencode":
            continue
        start = time.monotonic()
        decision = _run_opencode_tier(tier.model_id, tier.variant, message)
        elapsed = time.monotonic() - start
        if decision is not None:
            results.append({
                "display_name": tier.display_name,
                "status": "SUCCESS",
                "latency_s": round(elapsed, 2),
                "snippet": json.dumps(decision)[:300],
            })
        else:
            results.append({
                "display_name": tier.display_name,
                "status": "FAILURE (see warnings above)",
                "latency_s": round(elapsed, 2),
                "snippet": "",
            })

    print("\n=== Live verification results (REAL production-shaped prompt) ===")
    print(f"{'Model':<28} {'Status':<30} {'Latency (s)':<12} Snippet")
    for r in results:
        print(f"{r['display_name']:<28} {r['status']:<30} {r['latency_s']:<12} {r['snippet']}")


if __name__ == "__main__":
    main()
