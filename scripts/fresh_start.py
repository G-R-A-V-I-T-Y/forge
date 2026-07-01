"""scripts/fresh_start.py — Reset the desk and seed all 8 initial agents.

WARNING: This deletes ALL existing data in the database.
Run only when you want a clean start.
"""
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from store.db import get_connection, init_schema
from meta.spawner import spawn_agent

DB_PATH = PROJECT_ROOT / "data" / "forge.db"
STARTING_BALANCE = 50_000.0

SEED_AGENTS = [
    (
        "iron_moth",
        "# iron_moth — Thesis v1: Funding Rate Mean Reversion\n\n"
        "## Edge Hypothesis\n\n"
        "Persistent one-directional funding creates mechanical squeeze pressure. "
        "When funding rates stay negative for multiple consecutive 8-hour periods, "
        "short covering accelerates as the cost of holding becomes prohibitive, "
        "driving price up even without a fundamental catalyst.\n\n"
        "## Entry Conditions\n\n"
        "- Funding rate ≤ -0.03% for current period\n"
        "- Funding negative in at least 2 of the prior 3 periods\n"
        "- Price not already rallied >3% in the last 4h\n"
        "- OI has not fallen >10% in 24h\n\n"
        "Direction: Long (fade the short squeeze). "
        "Leverage: 3x. Position size: 10%.\n"
    ),
    (
        "jade_hawk",
        "# jade_hawk — Thesis v1: Liquidation Cascade Fade\n\n"
        "## Edge Hypothesis\n\n"
        "Post-cascade price action reverts as the market absorbs the move. "
        "Large liquidation events create mechanical overshoot — the initial "
        "impulsive move is followed by mean reversion as the book rebalances "
        "and counterparty flow absorbs the liquidated positions.\n\n"
        "## Entry Conditions\n\n"
        "- Single-asset liquidation volume > $10M in 1 hour\n"
        "- Price moved >3% in the direction of liquidations\n"
        "- Price shows reversal candle pattern on 15m chart\n"
        "- OI is stable or recovering (not still collapsing)\n\n"
        "Direction: Counter-trend. Leverage: 3x. Position size: 10%.\n"
    ),
    (
        "silver_basin",
        "# silver_basin — Thesis v1: Cross-Asset Lag\n\n"
        "## Edge Hypothesis\n\n"
        "SOL and ARB lag BTC on breakouts by 10–20 minutes. By monitoring "
        "BTC for an initial breakout signal and then entering the correlated "
        "altcoin, the agent captures the follower move with better risk/reward.\n\n"
        "## Entry Conditions\n\n"
        "- BTC breaks above a key 15m level (1.5× ATR move in 2 candles)\n"
        "- SOL/ARB has not yet moved >1% in the same direction\n"
        "- The asset's correlation to BTC is >0.70 over the last 20 periods\n"
        "- Funding rate on the altcoin is neutral (±0.01%)\n\n"
        "Direction: Same as BTC. Leverage: 4x. Position size: 10%.\n"
    ),
    (
        "copper_vane",
        "# copper_vane — Thesis v1: OI Divergence\n\n"
        "## Edge Hypothesis\n\n"
        "Price rising while OI falling = weak move, fading it is profitable. "
        "Open interest reflects conviction behind the price move. Divergence "
        "between price direction and OI direction indicates the move lacks "
        "broad market participation and is likely to reverse.\n\n"
        "## Entry Conditions\n\n"
        "- Price moved >2% in one direction over 4 hours\n"
        "- OI fell >5% during the same period\n"
        "- No major liquidation event to explain the OI drop\n"
        "- Price near a 4h resistance/support level\n\n"
        "Direction: Fade the price move. Leverage: 3x. Position size: 12%.\n"
    ),
    (
        "gray_finch",
        "# gray_finch — Thesis v1: Session Momentum\n\n"
        "## Edge Hypothesis\n\n"
        "US equities open (14:30 UTC) drives crypto correlation burst. "
        "The first 15m candle after the US open is where the strongest "
        "directional impulse occurs; trading that breakout has positive EV.\n\n"
        "## Entry Conditions\n\n"
        "- Current time is 14:25–14:35 UTC (US equity open window)\n"
        "- BTC has been ranging for at least 30 minutes pre-open\n"
        "- The first 1m candle at 14:30 breaks the pre-open range by >0.3%\n"
        "- Volume on the breakout candle is >2× the previous 5 candles' average\n\n"
        "Direction: Direction of the breakout. Leverage: 4x. Position size: 10%.\n"
    ),
    (
        "amber_wolf",
        "# amber_wolf — Thesis v1: Volatility Compression\n\n"
        "## Edge Hypothesis\n\n"
        "ATR contracts for N candles then expands — trade the expansion "
        "direction. Periods of low volatility are followed by volatility "
        "expansion; the direction of the expansion can be predicted by "
        "examining order book imbalance and funding rate bias.\n\n"
        "## Entry Conditions\n\n"
        "- 15m ATR has contracted for 20+ consecutive periods\n"
        "- ATR is in the bottom 20% of its 14-day range\n"
        "- Order book shows >60% bid or ask dominance (imbalance signal)\n"
        "- Funding rate has a directional bias (>0.005% or < -0.005%)\n\n"
        "Direction: Order book and funding bias. Leverage: 5x. Position size: 12%.\n"
    ),
    (
        "steel_crane",
        "# steel_crane — Thesis v1: Dominance Rotation\n\n"
        "## Edge Hypothesis\n\n"
        "BTC dominance dropping while BTC price stable = altcoin capital "
        "rotation signal. When BTC holds its value but its share of total "
        "crypto market cap declines, it signals capital flowing into altcoins.\n\n"
        "## Entry Conditions\n\n"
        "- BTC dominance declined >1% in the last 24h\n"
        "- BTC price is within ±2% of where it was 24h ago\n"
        "- The top 10 altcoins (ex-BTC) are all green (+1%+) in the last 4h\n"
        "- At least 3 altcoins show above-average volume vs their 14-day average\n\n"
        "Direction: Long the top-3 alts by volume. Leverage: 3x. Position size: 8% each.\n"
    ),
    (
        "onyx_heron",
        "# onyx_heron — Thesis v1: Open\n\n"
        "## Edge Hypothesis\n\n"
        "This agent starts with no fixed thesis. Its initial task is to "
        "review the full trade bank, study all other agents' strategies "
        "and performance, and generate a novel thesis from scratch based "
        "on observed patterns in the data.\n\n"
        "Until a thesis is generated, this agent will wait and observe.\n"
    ),
]


def main():
    print("=" * 60)
    print("FORGE — Fresh Start")
    print("=" * 60)
    print()
    print("This will DELETE ALL EXISTING DATA and seed 8 new agents.")
    print()

    if len(sys.argv) > 1 and sys.argv[1] == "--yes":
        confirmed = True
    else:
        resp = input("Are you sure? (y/N): ").strip().lower()
        confirmed = resp in ("y", "yes")

    if not confirmed:
        print("Cancelled.")
        return

    # Wipe existing DB
    if DB_PATH.exists():
        DB_PATH.unlink()
        print(f"Deleted existing database: {DB_PATH}")
    else:
        print("No existing database found.")

    # Initialize fresh schema
    conn = get_connection(str(DB_PATH))
    init_schema(conn)

    # Seed agents
    for name, thesis in SEED_AGENTS:
        agent = spawn_agent(
            conn, name, thesis,
            status="rookie",
            starting_balance=STARTING_BALANCE,
        )
        print(f"  Created agent: {name} (status={agent['status']}, balance=${STARTING_BALANCE:,.0f})")

    conn.close()

    # Verify
    conn = get_connection(str(DB_PATH))
    count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    bal_count = conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE mode='paper'"
    ).fetchone()[0]
    conn.close()

    print()
    print(f"Done. {count} agents seeded, {bal_count} account snapshots created.")
    print()
    print("Ready. Run: python forge.py")


if __name__ == "__main__":
    main()
