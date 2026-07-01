"""One-off manual verification script — NOT part of the pytest suite.

Calls all 6 remote opencode-routed tiers in llm/model_chain.py's CHAIN for
real (no mocking), one call each, with a short trading-decision-shaped
prompt, and records success/failure, latency, and a snippet of the
response for each. This is the live verification the captain explicitly
asked for: "test to make sure that our system can reliably call each of
the models in the list."

Deliberately not a pytest test: these are real network calls to
rate-limited free-tier models and a paid OpenRouter credential — slow,
occasionally flaky, and unsuitable for CI. Run manually:

    python scripts/verify_model_chain_live.py

Results are pasted into the PR description and summarized in
docs/superpowers/specs/2026-07-01-model-fallback-chain-design.md.
"""
import json
import subprocess
import time

from llm.model_chain import CHAIN, OPENCODE_TIMEOUT_SECS, _run_opencode_tier

TEST_SYSTEM_PROMPT = (
    "You are a professional discretionary trader at Forge, a quantitative "
    "prop trading firm trading crypto perpetuals. You think in expected "
    "value and manage risk carefully. Output JSON only. No prose outside "
    "of JSON."
)
TEST_DECISION_PROMPT = (
    "SOL-PERP is at $145.20. Funding has been negative for 3 consecutive "
    "8h periods. You have no open positions.\n\n"
    "Output JSON only, matching this exact schema "
    '(fill in a real one-sentence reason, keep the field names as-is): '
    '{"action": "wait", "reason": "<your one-sentence reason>"}'
)


def main() -> None:
    message = f"{TEST_SYSTEM_PROMPT}\n\n{TEST_DECISION_PROMPT}"
    results = []
    for tier in CHAIN:
        if tier.kind != "opencode":
            continue
        start = time.monotonic()
        try:
            decision = _run_opencode_tier(tier.model_id, tier.variant, message)
            elapsed = time.monotonic() - start
            if decision is not None:
                results.append({
                    "display_name": tier.display_name,
                    "model_id": tier.model_id,
                    "status": "SUCCESS",
                    "latency_s": round(elapsed, 2),
                    "snippet": json.dumps(decision)[:200],
                })
            else:
                results.append({
                    "display_name": tier.display_name,
                    "model_id": tier.model_id,
                    "status": "FAILURE (see warnings above)",
                    "latency_s": round(elapsed, 2),
                    "snippet": "",
                })
        except Exception as exc:
            elapsed = time.monotonic() - start
            results.append({
                "display_name": tier.display_name,
                "model_id": tier.model_id,
                "status": f"EXCEPTION: {exc}",
                "latency_s": round(elapsed, 2),
                "snippet": "",
            })

    print("\n=== Live verification results ===")
    print(f"{'Model':<28} {'Status':<30} {'Latency (s)':<12} Snippet")
    for r in results:
        print(f"{r['display_name']:<28} {r['status']:<30} {r['latency_s']:<12} {r['snippet']}")


if __name__ == "__main__":
    main()
