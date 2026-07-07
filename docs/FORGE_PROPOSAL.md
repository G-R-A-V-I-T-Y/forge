# Forge
## An Evolutionary Prop Trading System

> *"Where strategies are tested until only the strongest survive."*

---

## Vision

Forge is a prop-trading-shop-in-software: a managed ecosystem of autonomous AI trader-agents, each running a distinct thesis on crypto perpetuals, each paper-trading its own isolated account, each subject to the same performance review that would end a real trader's career or earn them more capital. The meta-system spawns, evaluates, mutates, and culls agents the way a good prop firm manages its desk — with data, not hope.

The inspiration is dual. One half is a classic prop shop: a tight universe, strict risk rules, a P&L-first culture, and a process for identifying who actually has edge vs. who got lucky. The other half is the Slay the Spire observation: that a sufficiently capable AI, given the right context about the current game state, can identify non-obvious synergies and make counterintuitive decisions that a human expert would miss — and do so consistently, not just once. A screenshot of a deck, sent to AI, builds something the human player would never have conceived, with better synergies and a cleaner path to the top. Forge is that system for markets. Not one agent, one strategy, one lucky run — but a farm of them, competing, evolving, and graduating to live capital only when they've earned it.

---

## The Prop Shop Model

Every agent in Forge is a *trader*, not a rule engine. The framing matters. Agents are given a professional trader persona and full awareness of how they will be evaluated. They know their job is to:

- Develop and maintain a coherent trading thesis with a specific edge hypothesis
- Execute trades that reflect that thesis with discipline
- Review their own performance honestly, including losses
- Update their thesis based on evidence, not emotion
- Survive long enough to prove the edge is real

The meta-controller is the *Head of Desk*. It reviews performance, allocates attention (compute), culls losing traders, and seeds new ones from promising patterns in the historical record. There are hard desk-wide rules that no trader can violate — the equivalent of a firm's risk management function, which no individual P&L ever justifies overriding.

This framing isn't cosmetic. Giving the LLM a professional trader persona — with explicit awareness of its evaluation criteria and the consequences of underperformance — measurably shifts the distribution of its reasoning toward careful, structured, probabilistic thinking rather than generic chatbot output.

---

## The Central Design: Three-Loop Desk

A critical lesson from running the system: the LLM is deployed at the wrong timescale. Having a 35B model re-read thousands of numbers every 5 minutes to recompute what a z-score means is the weakest possible use of LLM intelligence — slow, expensive, noisy, and impossible to backtest. The strongest use is the reflection loop: LLMs should do the *slow thinking* (hypothesis generation, strategy synthesis, postmortem reasoning, regime interpretation) and emit *fast artifacts* (executable, backtestable strategy specs) that trade mechanically.

**The consequence: split every agent into a slow mind and a fast body.**

- The *mind* (LLM, runs at reflection cadence — hours/days): owns the thesis, reads the trade bank and backtests, does research, writes and revises a **strategy spec** — a constrained, executable artifact (signals, thresholds, entry/exit/sizing rules, regime filters).
- The *body* (deterministic Python, runs at heartbeat cadence): executes the spec mechanically. Fast, free, consistent, and — critically — **backtestable**, so every proposed thesis revision is validated against history *before* it risks even paper capital.

This is not abandoning the vision; it is the prop-shop model taken seriously. Real traders don't recompute their indicators by hand every 5 minutes — they design a playbook and execute it with discipline, revising it in the evening.

And because the question deserves an empirical answer rather than an architectural decree: **keep 2–3 pure LLM-decides agents in the arena** (with pinned models, temperature 0, and full decision logging) as a control arm. If the reasoning agents beat the compiled agents on risk-adjusted net PnL over 500+ trades, the ecosystem will say so. Forge's whole point is that the market decides.

```
┌────────────────────────────────────────────────────────────────────┐
│ SLOW LOOP — Evolution (hours–days)  · frontier LLM (Claude)        │
│  reflection: trade bank + counterfactuals + backtests + research   │
│  → revised STRATEGY SPEC (versioned, validated, diffable)          │
│  meta-controller: evaluate vs null model · cull · spawn · graveyard│
└──────────────┬─────────────────────────────────────────────────────┘
                │ deploys spec (only after walk-forward validation)
┌──────────────▼─────────────────────────────────────────────────────┐
│ MID LOOP — Desk Risk Officer (30–60 min) · local LLM               │
│  regime synthesis, event awareness, gross-exposure throttle,       │
│  per-agent enable/disable, emergency posture. Cannot ADD risk.     │
└──────────────┬─────────────────────────────────────────────────────┘
                │ risk budget / kill flags
┌──────────────▼─────────────────────────────────────────────────────┐
│ FAST LOOP — Execution (5 min heartbeat, later WS) · pure Python    │
│  compiled agents: evaluate spec → orders                           │
│  LLM-control-arm agents: pinned model, temp 0, decision logging    │
│  risk gate (hardened) → paper/live bridge → fingerprints           │
└────────────────────────────────────────────────────────────────────┘
        ▲                                    │
        │   git-native ledger  ◄──────────────┘ every heartbeat appends
        │   (candles, funding, OI, liqs, decisions — backtest substrate,
        │    committed to git every cycle — see Data Layer & Ledger below)
```

**The strategy spec is the pivotal new artifact.** Not free code (unsafe, unverifiable) and not a rigid config (kills expressiveness): a constrained JSON/YAML DSL over the heartbeat's feature vocabulary — entry conditions as weighted evidence terms (the theses already use exactly this shape!), exit rules, sizing curve, regime filters, max hold, per-asset scope. The existing thesis markdown remains the human-readable "why"; the spec is its executable shadow. The LLM writes both; the backtester referees; only validated specs deploy.

---

## Arena: Crypto Perpetuals

### Why Crypto Perpetuals

| Property | Benefit |
|---|---|
| 24/7 market | 3–15 trades/day achievable; statistical signal in 1–2 weeks, not months |
| Funding rates | An entire signal class unique to perps — exploitable and measurable |
| Liquidation data | Public, real-time cascade signals unavailable in any other market |
| Leverage (up to 10×, hard cap) | Amplifies edge without infinite-risk structures |
| Correlated universe | BTC, ETH, SOL move together and diverge predictably — rich cross-asset signals |
| Single exchange | Data, paper trading, and live trading all from one API — no seams |

### Why Not Forex

Retail forex brokers are mostly market makers with wide spreads and opaque order flow. No unified order book, no public liquidation data, no funding rate signal, fragmented data ecosystem. Crypto perpetuals are strictly superior for this architecture.

### Exchange: Hyperliquid

Forge is built on **Hyperliquid** as the single exchange for data, paper trading, and live trading. There are no seams between environments — paper-to-live is a config flag, not a platform migration.

**Why Hyperliquid:**
- On-chain order book — fully transparent, no market-maker opacity
- Rich API: OHLCV, funding rates, open interest, liquidation history, order book depth — all public, all free
- 100+ perpetual markets with meaningful liquidity
- Testnet (`testnet.hyperliquid.xyz`) and mainnet (`hyperliquid.xyz`) are structurally identical — same API, same data format, different endpoint
- Non-custodial (wallet-based) — different regulatory category from centralized exchanges
- No KYC requirement — accessible to US users

**Paper trading model:** Agents interact with the real Hyperliquid API for market data only. Paper trades are simulated in-process against real Hyperliquid prices (bid/ask at time of signal). No testnet orders are submitted. When an agent goes live, the same decision is routed to the real Hyperliquid order API. Agent logic is unchanged.

**US / Washington State note:** Hyperliquid is a decentralized protocol. It is non-custodial and operates without KYC. US persons accessing Hyperliquid do so at their own discretion. The regulatory landscape for non-custodial DeFi protocols differs materially from regulated centralized exchanges. Consult legal counsel before deploying real capital.

### Trading Universe

**20 assets (config.yaml `universe`, updated by Head of Desk):**

BTC, ETH, SOL, SUI, AVAX, LINK, AAVE, BNB, ARB, OP, TAO, FET, RENDER, XRP, XLM, TIA, HYPE, LTC, BCH, ADA

Large enough to support dozens of distinct, non-overlapping strategies. Small enough to monitor quality signals across all assets without meaningful API cost. Sector grouping (L1, L2, Modular/DA, DeFi/Oracle, AI, Exchange, Legacy Payments) is defined in `market/heartbeat.py`'s `SECTORS` map and used for sector-relative-strength theses (`iron_moth`) and cross-sectional breadth.

### Most Likely Money-Making Paradigms

Ranked by (structural persistence × fit to Forge's architecture × evidence in the literature and this DB):

1. **Funding harvest / funding dislocation** — The one edge in this market that is *paid, not predicted*: extreme funding is a cash flow you collect while positioned against a crowd that must eventually pay to stay. Self-refreshing (leverage demand never dies), measurable.
2. **Liquidation cascade fade** — Forced flow is the most mechanical inefficiency in perps: liquidations are price-insensitive sellers/buyers, and the overshoot-revert pattern is well documented. Requires the real liquidation feed.
3. **Event/unlock positioning** — Token unlocks and listings are scheduled, public, and repeatedly under-anticipated in mid-caps. Perfect for LLM reasoning (each event has idiosyncratic structure) at slow cadence with mechanical execution.
4. **Cross-sectional momentum / relative value at 4h–daily horizons** — Real but crowded; survives fees only at longer horizons and modest turnover. Keep, but force holding periods ≥ 12h.
5. **Regime/vol transition trading** — Legitimate as *filters and modulators* for the whole desk more than as standalone alpha.
6. **Microstructure at 5-min LLM cadence** — Structurally unviable as built. Retire or rebuild on streaming data much later.

Portfolio-level: the money-maker profile for Forge v1 is **a book that is mostly market-neutral-ish carry (funding) + episodic convexity (cascade fades, events), with momentum as the diversifier** — not 10 directional LLM day-traders. Daily-PnL-positive is a carry-book property, not a prediction-book property.

---

## Target Performance

| Metric | Individual Agent Target | Portfolio Target (ensemble) |
|---|---|---|
| Annual return | 10–25% | 15–30% |
| Max drawdown | 8–15% | 3–6% |
| Win rate | >55% | — |
| Profit factor | >1.4 | — |
| Sharpe ratio | >1.0 | >1.5 |
| Trade frequency | 3–15 per day | — |

The portfolio drawdown is structurally lower than any individual agent's drawdown because agent equity curves are weakly correlated with each other and with BTC. Sizing live allocations in proportion to Sharpe ratio suppresses portfolio drawdown further.

The target numbers are aspirational, not engineering requirements. The system finds whatever edge exists and compounds it. A realistic v1 success is: *funding-carry book yielding 10–25% annualized on small capital with max DD < 10%, plus optionality from event/cascade agents* — and, more importantly, a machine that produces and validates new strategies faster than old ones decay. That second thing is the actual asset being built.

---

## System Architecture

```
                     HYPERLIQUID API
                   (market data, REST)
                          │
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
   ┌───────────┐   ┌───────────┐   ┌───────────┐
   │ Agent A   │   │ Agent B   │   │ Agent N   │
   │ compiled  │   │ compiled  │   │ compiled  │
   │ (fast)    │   │ (fast)    │   │ (fast)    │
   └─────┬─────┘   └─────┬─────┘   └─────┬─────┘
         │               │               │
         └───────────────┼───────────────┘
                         │ execution orders
                         ▼
                  ┌──────────────┐
                  │  RISK GATE   │  ← non-bypassable, hardened
                  └──────┬───────┘
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
       ┌─────────────┐     ┌──────────────┐
       │ PAPER BRIDGE│     │  LIVE BRIDGE │
       │(sim vs HL   │     │ (real HL API)│
       │ real prices)│     └──────────────┘
       └──────┬──────┘
              │
              ▼
     ┌─────────────────┐
     │  forge.db (local,│  ← disposable, gitignored, fast read/write cache
     │  disposable)     │     rebuildable from the ledger at any time
     └────────┬────────┘
              │ append_ledger_record() on every decision/candle/trade/close
              ▼
     ┌─────────────────┐
     │  ledger/ + state/│  ← git-tracked, committed + pushed every cycle
     │  (source of truth)│    system of record — see Data Layer & Ledger below
     └────────┬────────┘
              │
              ▼
     ┌─────────────────┐
     │  META-CONTROLLER│  ← evaluate vs null · cull · spawn
     │  HEAD OF DESK   │  ← synthesis, chat interface
     └────────┬────────┘
              │
              ▼
     ┌─────────────────┐
     │  RISK OFFICER   │  ← mid loop: regime, exposure, kill flags
     └─────────────────┘
              │
              ▼
     ┌─────────────────┐
     │   WEB DASHBOARD │  ← localhost:8000
     └─────────────────┘
```

### Data Layer & Ledger: What Gets Stored, How, and Why

**Hard constraint: everything — code and data — fits in one git repo. No external database server, no cloud data warehouse.** A burned laptop, replaced and `git pull`ed, resumes exactly where it left off. This is a real engineering constraint, not an aspiration: naively committing an ever-mutating SQLite file to git (the original plan) blows up repo size, because git can't delta-compress random page rewrites the way it deltas text. The resolution — implemented and running — is a **git-native, append-only ledger** that separates the local operational cache (fast, mutable, gitignored, disposable) from the git-tracked source of truth (append-only, git-friendly, durable). Full design rationale: `docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md`.

**Two layers:**
- **`data/forge.db`** — the local, gitignored, disposable SQLite cache. Fast reads/writes for the live trading loop. Never the system of record; can be deleted and rebuilt at any time.
- **`ledger/` + `state/`** — the git-tracked source of truth, committed and pushed every heartbeat cycle by `store/git_sync.py` (best-effort, non-blocking — a failed push just retries next cycle, since git push always carries every unpushed commit forward).

**What's captured every heartbeat cycle, and why raw (not derived):** only raw inputs are stored — computed indicators (z-scores, correlations, regime tags) are recomputed from raw inputs at read time by the backtester, never trusted as frozen historical fact. This matters for correctness, not just size: a later bug fix to a feature calculation would otherwise silently corrupt every backtest replaying "history" through the old, wrong values. The one deliberate exception is the **decisions** stream, which must capture exactly what the agent saw and acted on — warts included — because the goal there is calibrating the agent, not re-deriving a cleaner version of history.

| Ledger stream | Cadence | Why raw (not recomputed) | Size / 90 days |
|---|---|---|---|
| `candles_5m` | 5 min × 20 assets | Re-fetchable from the exchange, but a local copy makes backtests self-contained | ~10MB |
| `funding` | hourly × 20 | Same | ~0.5MB |
| `oi` (open interest) | 5 min × 20 | **Not retroactively available** — miss it live, it's gone | ~7MB |
| `liquidations` | event-driven | Same — proxy/live-only unless paying for Coinalyze | ~0.4MB |
| `decisions` | 5 min × every agent, **wait included, not just enter** | The selection-bias fix: a "wait" decision carries the same structured `confidence`/`evidence_strength` as an "enter", so the reflection loop can calibrate on the full sample, not just the ~10% of cycles that traded | ~39MB (dominant stream — structured evidence, not prose) |
| `trades` | per closed trade | Full entry+exit fingerprint (OHLCV/funding blobs excluded — redundant with `candles_5m`/`funding`, and raw bytes don't round-trip through JSON) | ~4MB |
| `accounts` | per trade close | Balance/peak snapshot | negligible |
| **Total** | | | **~63MB / 90 days ≈ 250MB/year** |

At this rate, 5 years of continuous operation is ≈1.3GB — comfortably inside GitHub's practical range. If it ever grows faster than expected (verbose reasoning creeping back into `decisions`, universe size growing 10×), the escalation ladder, in order: tighten retention on the biggest stream first (decisions); resolution-decay old `candles_5m`/`oi` to hourly beyond a rolling window (already implemented, default 12 months); shard by year into separate repos; Git LFS as a last resort for any single partition risking GitHub's 100MB/file hard limit.

**Storage method — hot JSONL, cold Parquet:**
```
ledger/
  decisions/2026-07.jsonl        ← hot: current month, appended every cycle (pure byte-level
                                     append — the most git-friendly write pattern there is)
  decisions/2026-06.parquet      ← cold: closed month, compacted (columnar, 3-5× smaller)
  candles_5m/, candles_1h/, candles_4h/, funding/, oi/, liquidations/, trades/, accounts/
  events.jsonl                   ← event calendar, small, never partitioned
state/
  current.json                   ← open positions, live balances, agent status — small,
                                     overwritten + committed every cycle (NOT history —
                                     "right now", so a fresh clone restores the exact last
                                     heartbeat's state, not "state as of last commit")
data/forge.db                    ← gitignored, disposable local cache — never the system of record
```
`scripts/compact_ledger.py` runs monthly (scheduled in `forge.py`, cron `day=1, hour=3 UTC`): converts the prior month's closed JSONL to Parquet, downsamples `candles_5m`/`oi` older than 12 months to hourly, and is idempotent and fault-isolated per file (one malformed partition doesn't abort the batch).

**Example ledger record shapes:**
```json
// ledger/decisions/2026-07.jsonl — one line per agent per cycle
{"ts":"2026-07-06T19:35:00Z","agent":"sage_turtle","action":"wait",
 "confidence":0.35,"evidence_strength":{"unlock_size":0.0,"days_to_event":0.3},
 "model":"qwen3.6-35b","reason":"days_to_event too far"}

// ledger/candles_5m/2026-07.jsonl — one line per asset per cycle
{"ts":"2026-07-06T19:35:00Z","asset":"BTC-PERP","o":64900.0,"h":65100.0,"l":64800.0,"c":65000.0,"v":12.5}
```

**Disaster recovery:** `python scripts/rebuild_local_cache.py` reconstructs `data/forge.db` from `ledger/` + `state/current.json` alone — agents, closed trades, account balances (state's snapshot wins as authoritative for "current," regardless of ledger completeness), and open positions (a minimal synthetic "open" trade row is derived from the position snapshot itself, since `execute_close` only ever ledger-exports on close). This is the literal proof of "burned laptop → git pull → back to normal." Deliberately **not** restored to the local cache: `decisions` and market-data history — they're analytical/calibration archives with no operational-hot-path consumer (the nightly counterfactual job and any decision-history UI aren't on the "must resume trading immediately" path), and the SQLite `decisions` table has no columns for `confidence`/`evidence_strength`/`model` to receive them anyway. That history isn't lost — it's queryable directly from the ledger files.

**Additional data sources (not yet built — M7b):**
- **Real liquidation feed** (WS flag or Coinalyze): the single highest-value feed for liquidation-cascade strategies; would land in the existing `liquidations` ledger stream
- **Event calendar**: FOMC/CPI datetimes (static quarterly file), **token unlock schedules** (predictable forced supply), exchange listing announcements — would land in `ledger/events.jsonl`
- **Cross-exchange context**: Binance/Bybit funding + basis for the same assets — dislocations between venues are cleaner mean-reversion signals than absolute levels

### Persistence: SQLite tables (forge.db, disposable local cache)

- `agents` — agent registry (name, status, spawn date, cull date, config, pinned model)
- `theses` — all thesis versions, all agents, including terminated ones
- `trades` — full fingerprint for every trade ever made (closed trades also ledger-exported)
- `decisions` — every heartbeat cycle: agent, timestamp, action (enter/wait/close/risk_blocked/error), reason, confidence, model, prompt hash, top-N candidate assets (also ledger-exported, with `confidence`/`evidence_strength` — see Data Layer above)
- `accounts` — per-agent account balance history (paper and live; also ledger-exported)
- `positions` — currently open positions (all agents, for desk-wide position visibility; captured in `state/current.json` every cycle)
- `reflections` — log of every thesis reflection: evidence, research, proposed changes, adversarial critique, outcome
- `evaluations` — meta-controller evaluation results per agent per cycle
- `settings` — desk-wide and per-agent settings (editable from web UI)
- `chat_history` — head-of-desk conversation history
- `live_trades` — immutable append-only record of all real money trades

### Risk Gate

Stateless Python validator. Every trade decision passes through it before execution. Non-bypassable by agent logic.

**Hard rules:**
- Stop loss / take profit geometry: for long, SL < entry < TP; for short, TP < entry < SL; TP non-null and above fee hurdle; entry within ~0.5% of current heartbeat price; reward:risk ≥ 0.5
- Maximum leverage: 10× hard cap (configurable lower per agent or desk-wide)
- Maximum position size: 20% of account per trade (configurable lower)
- Maximum notional exposure: size × leverage capped at desk-defined limit (replaces separate size/leverage caps)
- Maximum concurrent open positions per agent: 3
- Drawdown kill: if agent account drops >15% from peak, all positions closed, agent suspended

No exceptions. Not for high-confidence trades. Not for "exceptional" market conditions.

### Trading Bridge

```python
class TradingBridge(ABC):
    def enter(self, order: Order) -> Fill: ...
    def get_positions(self) -> list[Position]: ...
    def close(self, position_id: str, reason: str) -> Fill: ...
    def get_account(self) -> AccountState: ...

class PaperBridge(TradingBridge):
    # Simulates fill: price ± spread/2 ± slippage_estimate by side
    # Accrues funding on open positions; writes duration_minutes
    # Paper is pessimistic — the promotion decision depends on it
    # close() ledger-exports the full closed trade + account snapshot
    # (store/positions.py's execute_close — shared by paper and live)

class LiveBridge(TradingBridge):
    # Submits real orders to Hyperliquid mainnet API
```

Agents never know which bridge is active. Promoting to live is a config change. No agent code changes.

**Shadow mode:** Before full live promotion, agent runs both bridges simultaneously. Paper fills and live fills are compared for slippage and timing. Human reviews before confirming full live.

---

## The Trader-Agent

### Persona

Each agent is initialized with a professional trader persona. The system prompt begins:

```
You are a professional discretionary trader at Forge, a quantitative prop 
trading firm trading crypto perpetuals. Your account is $50,000. You keep it 
all and grow it — or you get cut.

Your edge is your thesis: a specific, well-reasoned hypothesis about a 
market inefficiency you can exploit reliably across varying conditions. 
You built it. You own it. You update it when the evidence demands.

You are evaluated on:
  Win rate              (target: >55%)
  Profit factor         (target: >1.4)
  Avg win / avg loss    (target: >1.2)
  Weekly return         (target: positive)
  Max drawdown          (hard limit: 15%)
  Sharpe ratio          (target: >1.0)
  Trade frequency       (target: 3–15 per day

You think in expected value. A 40% win rate with 3:1 avg win/loss is 
profitable. A 70% win rate with 0.5:1 win/loss is a slow bleed. You know 
this. You design every trade around it.

You do not overtrade. You do not take trades that don't fit your thesis. 
You do not let a losing streak make you reckless or a winning streak make 
you sloppy. You have one job: find your edge, express it cleanly, and 
let it compound.
```

### What the Agent Knows at Decision Time

**1. Its thesis (current version)**

The agent's full living strategy document.

**2. Its strategy spec (executable shadow)**

A constrained JSON/YAML artifact derived from the thesis: entry conditions as weighted evidence terms, exit rules, sizing curve, regime filters, max hold, per-asset scope. The compiled agent body evaluates this spec mechanically at each heartbeat.

**3. Its aggregate performance**

```
PERFORMANCE SUMMARY — jade_hawk — thesis_v9
─────────────────────────────────────────────
Account:    $52,340  (+4.7% all-time)
Peak:       $53,100  |  Current DD: -1.4%

ALL TIME (87 trades, 21 days):
  Win rate:       61.0%    Profit factor:  1.52
  Avg win:       +1.8%     Avg loss:      -1.1%
  Avg W/L ratio:  1.63     Sharpe:         1.71
  Best trade:    +4.2%     Worst trade:   -2.1%

LAST 20 TRADES:
  Win rate: 65.0%  |  PF: 1.88  |  Trend: ↑ improving

LAST 7 DAYS:
  Trades: 31  |  Return: +2.3%  |  Max intraday DD: -0.8%

BY REGIME:
  trending_bull:  71% WR (28 trades)
  range_low_vol:  48% WR (31 trades)  ← underperforming
  trending_bear:  55% WR (18 trades)
  range_high_vol: 60% WR (10 trades)

VS NULL MODEL:
  random_walk:  50% WR (Sharpe 0.00) — agent +0.71 Sharpe
  btc_hold:     +3.2% return — agent +4.7% return
```

**4. Last 10 closed trades (with outcomes and postmortems)**

Asset, direction, entry, exit, P&L%, duration, thesis version, the hypothesis text at entry, outcome, and the agent's own one-sentence postmortem.

**5. Current open positions (own)**

Entry price, current P&L, distance to SL/TP, time open.

**6. Desk positions (all other active agents)**

```
DESK POSITIONS (other traders):
  iron_moth:    LONG ETH  @ $3,540  (+0.8%)  — entry 2h ago
  silver_basin: LONG SOL  @ $145.20 (+1.2%)  — entry 4h ago
  copper_vane:  [no positions]
  gray_finch:   SHORT BTC @ $65,100 (-0.3%)  — entry 45m ago
```

Agents may hold positions in the same asset as other agents, including opposing directions. Competing positions represent divergent theses and provide natural portfolio hedging.

**7. Current market state**

- OHLCV: last 40 candles of primary timeframe for all 20 assets (heartbeat packet; historical continuity from the `ledger/candles_5m/` stream)
- Funding rates: current + last 24h per asset (z-score vs 14d baseline)
- Open interest: 24h change per asset
- Liquidation volume: last 4h per asset (from real feed, not proxy)
- BTC dominance
- 20-period correlation matrix across the universe
- Current market regime tag
- Event calendar: next macro event within 48h

**8. Decision prompt**

```
Based on your thesis, your performance record, and current market conditions, 
make a decision. You may:
  - Enter a new trade (specify all parameters)
  - Adjust stop/take-profit on an open position
  - Close a position early (with reason)
  - Do nothing (with a brief note on why)

If entering, explain your reasoning in terms of your thesis, the specific 
conditions that make this setup compelling, and your expected value estimate.
Output JSON.
```

### Model Attribution: Pinned Models

Each agent has one pinned model, recorded as part of agent config. Fallback only *within* the same model (retry), never *across* models for trading decisions. The local Qwen server (12–20s/decision with reasoning off) is the right default body for compiled agents. Frontier-model budget (Claude) is reserved for the reflection loop where per-call value is 100× higher. If model identity is interesting, make it an explicit experiment: same thesis × two models = two agents.

Every decision logs `model`, `temperature`, and prompt hash.

### Internet Research During Reflection

When an agent enters a thesis reflection cycle, it has access to web search. This is strictly a thesis-update tool — not available during live decision cycles (too slow, too noisy).

Use cases during reflection:
- "What does published research say about funding rate mean reversion regimes?"
- "Are there known SOL network events that correlate with price anomalies?"
- "What are professional crypto traders discussing about current BTC dominance structure?"

Research output is summarized and attached to the reflection. The updated thesis incorporates both performance data and research findings.

### Querying the Full Historical Record

During both live decision cycles and reflection, agents have access to the full historical trade bank — every trade ever made by every agent, including terminated ones.

During decisions, an agent can request: "Show me all trades by any agent where funding was < -0.04% on SOL and OI was falling — what was the collective win rate?"

During reflection, this query capability is central: "What conditions in the historical record produced the best outcomes for assets in my universe? What conditions consistently produced losses?"

The Head of Desk ensures no new seed thesis repeats a strategy that was previously terminated. The full graveyard is part of its institutional knowledge.

### The Decisions Table

The trade bank records only actions taken. The reflection loop will therefore learn from a censored sample — it can never discover "I skip too many good setups" or "my confidence is uncalibrated at 0.55–0.65" because the counterfactuals were discarded.

A `decisions` table logs every heartbeat cycle: agent, timestamp, action (enter/wait/close/risk_blocked/error), reason, confidence, model, prompt hash, and top-N candidate assets considered. A nightly job fills in counterfactual outcomes for waits (what the entered-hypothetical would have returned at thesis-standard SL/TP). This turns every 5-minute cycle into training data instead of only the ~10% that trade.

---

## Trade Fingerprint Schema

The atomic unit of institutional memory. Written at entry, completed at close.

```json
{
  "trade_id": "jade_hawk_20250629_143712_SOL",
  "agent_id": "jade_hawk",
  "thesis_version": "v9",
  "spec_version": "spec_v3",
  "model_used": "qwen3:35b",
  "account_balance_at_entry": 52340.00,
  "mode": "paper",

  "asset": "SOL-PERP",
  "direction": "long",
  "entry_price": 145.20,
  "stop_loss_price": 143.40,
  "take_profit_price": 149.80,
  "leverage": 5,
  "position_size_pct": 0.12,
  "notional_usd": 6280.80,
  "entry_timestamp": "2025-06-29T14:37:12Z",

  "market_context": {
    "regime": "range_high_vol",
    "ohlcv_15m_40_candles": [[ts, o, h, l, c, v], ...],
    "ohlcv_1h_20_candles": [[ts, o, h, l, c, v], ...],
    "ohlcv_4h_10_candles": [[ts, o, h, l, c, v], ...],
    "funding_rate_current": -0.042,
    "funding_z_score_14d": -2.3,
    "funding_rate_8h_history": [-0.038, -0.041, -0.042],
    "open_interest_usd": 420000000,
    "open_interest_24h_change_pct": -3.2,
    "liquidation_volume_1h_usd": 8500000,
    "liquidation_direction_dominant": "long",
    "btc_dominance": 0.543,
    "correlation_sol_btc_20p": 0.82,
    "correlation_sol_eth_20p": 0.78
  },

  "agent_reasoning": {
    "hypothesis": "SOL funding has been negative for 3 consecutive 8h periods indicating sustained short pressure. $8.5M in long liquidations in the last hour — counterintuitive with negative funding — suggests a squeeze setup as trapped shorts face escalating cost. Price has held the 145 level on two 15m retests. Expect shorts to cover, pushing toward 149-150.",
    "key_conditions_met": ["persistent_negative_funding", "support_hold_15m", "long_liquidation_anomaly"],
    "key_conditions_missing": ["volume_confirmation_on_bounce"],
    "confidence": 0.68,
    "expected_value": "+0.9% EV: 65% assumed win rate × 4.6% TP − 35% × 2.4% SL"
  },

  "outcome": {
    "exit_price": 149.60,
    "exit_timestamp": "2025-06-29T16:52:44Z",
    "exit_reason": "take_profit",
    "duration_minutes": 135,
    "pnl_pct": 0.031,
    "pnl_usd": 1621.40,
    "result": "win",
    "agent_postmortem": "Setup played out cleanly. Funding squeeze materialized in 2h. Price hesitated at 148 for 20min before pushing through — stop never threatened. Clear thesis execution."
  }
}
```

OHLCV arrays are stored as compact binary (msgpack) in SQLite to keep file size manageable. A full fingerprint including three timeframe windows is approximately 8–12 KB. 10,000 total trades across all agents = ~100 MB — comfortably within git's practical limits.

---

## Anti-Overfitting in the Thesis Loop

### 1. Minimum Trade Threshold

No thesis update on fewer than 20 completed trades since the last update. Hard gate, no exceptions.

### 2. Evidence / Holdout Split

Reflection prompt is fed:
- **Evidence window**: trades 21–N (older, more stable signal)
- **Holdout window**: most recent 20 trades (withheld from thesis construction)

Proposed thesis is evaluated against the holdout. If it would have performed materially worse on the holdout than the old thesis, the update is flagged as overfit and blocked until 10 more trades accumulate.

### 3. Cross-Agent Validation

Before committing a thesis change, the system queries the full trade bank: "Do fingerprints from *other agents* support the pattern this agent is trying to encode?" An agent that wants to add a new primary condition sees the cross-agent win rate for that condition before it is accepted.

### 4. Update Throttle

Maximum one thesis update per 30 trades or 14 calendar days, whichever is later.

### 5. Pattern Persistence Requirement

A new *primary* signal condition must appear across at least 3 non-overlapping 7-day windows in the historical fingerprint data. Patterns from a single week are flagged as potentially regime-specific.

### 6. Adversarial Second Pass

After any thesis update, a second LLM call plays devil's advocate: "What conditions would cause this thesis to fail? What sample bias might be present? What is the weakest assumption?" Adversarial findings are appended to the thesis as "known weaknesses" and included in future decision context.

### 7. Regime Tagging

Every fingerprint is tagged with market regime (derived from BTC 30-day volatility + trend + dominance):
- `trending_bull`, `trending_bear`, `range_low_vol`, `range_high_vol`, `crisis`

Reflection always shows win rate broken down by regime. An agent whose thesis doesn't mention regime but whose wins cluster in one regime is explicitly flagged.

### 8. Walk-Forward Validation (M7b+)

The backtester enforces train/validate/test windows. Overfit metrics include deflated Sharpe ratio and parameter-sensitivity sweeps. Anti-overfit gates are *code*, wrapping the rules above around the backtester: min-trades, holdout, cross-agent validation, throttle, pattern persistence, adversarial pass (second LLM call attacking the spec), regime flags.

### 9. Calibration

Per-agent confidence vs realized win-rate curves are tracked and reported. The `confidence` column in the decisions table enables this: an agent that says "90% confidence" 80% of the time but wins 60% of the time is miscalibrated, and that calibration error is visible in the reflection log.

---

## Agent Lifecycle

```
SPAWN ──► ROOKIE (< 30 trades, no evaluation)
              │
              ▼
         EVALUATION (every 30 trades, by meta-controller)
              │
      ┌────────┴────────┐
      ▼                 ▼
   ACTIVE          SUSPENDED
   (metrics pass)  (borderline)
      │                 │
      │           REVIEW (Head of Desk)
      │                 │
      │         ┌───────┴───────┐
      │         ▼               ▼
      │    REACTIVATE       TERMINATE
      │                         │
      │                   HARVEST: 5 best fingerprints
      │                   → seed next spawn
      │                   → permanent graveyard record
      ▼
   PROMOTED
   (beats null model at 95%, 100+ trades, calibrated confidence, human review)
      │
      ▼
   SHADOW (paper + live simultaneously, 10 days)
      │
      ▼
   LIVE TRADING
```

**Culling triggers:**
- Profit factor < 0.8 for two consecutive evaluation cycles → suspend
- Max drawdown > 20% → immediate suspension
- Win rate < 35% after 50 trades → terminate
- Zero trades in 5 days → thesis review required

**Graveyard:** All terminated agents persist permanently in SQLite. Their full trade history, all thesis versions, reason for termination, and best-performing fingerprints are accessible to the Head of Desk and all active agents. No information is ever deleted.

**No repeat seeds:** The Head of Desk maintains awareness of all historical agent theses. A new seed is checked against the graveyard — if it is substantively similar to a strategy that was already tried and failed, it is rejected and a different seed is generated.

**Names:** Each agent is assigned a unique two-word name (adjective + animal: `jade_hawk`, `silver_basin`, `iron_moth`). Names persist through thesis versions and into the graveyard. This makes performance discussions human-readable.

**Evaluation cadence in trades, not days:** Statistics respect small samples. Compare each agent to the null distribution. Probation before termination.

---

## Benchmark Agents

Two permanent benchmark agents are seeded alongside all trading agents:

- `random_walk` — coin-flip entries, thesis-standard SL/TP and sizing, same risk gate. Establishes the floor.
- `btc_hold` — buy and hold BTC. Establishes the directional benchmark.

Every leaderboard metric is displayed *relative to the null distribution*. An agent is only "working" when it clears the random agent's 95th percentile over ≥100 trades.

---

## Seeding the First Cohort

| Agent | Strategy | Seed Hypothesis |
|---|---|---|
| `iron_moth` | Cross-sectional Momentum | Ranks all assets on multi-horizon returns (30m, 2h, 12h, 24h); enters top-ranked when momentum acceleration and volatility-adjusted returns confirm. Sector-relative momentum avoids beta crowding. |
| `silver_basin` | Funding Dislocation | Studies only funding rates: z-score vs 14d history, trend, predicted funding from OI, acceleration. Enters when funding is statistically irrational; exits on normalisation. Ignores price. |
| `copper_vane` | Open Interest Intelligence | OI×Price regime matrix: rising price + rising OI = genuine trend (go with); rising price + falling OI = short squeeze (fade); falling price + rising OI = new shorts (join); falling price + falling OI = capitulation (wait). |
| `steel_crane` | Liquidation Hunter | Monitors liquidation clusters, cascade history, leverage estimates, funding rates, OI changes. Enters when cascading liquidations and extreme leverage make a squeeze imminent — fades the cascade. |
| `onyx_heron` | Relative Value | Trades only spreads: SOL vs ETH, BTC vs ETH, AI-tokens vs L1 basket. Uses z-score, correlation, cointegration to identify cheap/rich legs. Long cheap, short rich — naturally beta-neutral. |
| `jade_hawk` | VWAP Mean Reversion | Fades price extremes relative to VWAP across 15m/1h/4h timeframes. Enters short when price > VWAP(1h) + 2*ATR, long when price < VWAP(1h) - 2*ATR. Opposite paradigm to the desk's momentum strategies. |
| `violet_lion` | Volatility Regime Trader | Trades volatility regime transitions. In compressed vol (coiling), enters breakout direction with microstructure confirmation. In expanded vol, fades the emotional extreme. Produces directional trades from vol state changes. |
| `crimson_fox` | Session Pattern Arbitrage | Exploits predictable intraday patterns across global sessions (US Open, US Reversal, Asian Drift, weekly patterns, pre-settlement windows). Low edge per trade but highly reliable compounding. Uses time, not price/volume/OI, as primary signal. |
| `sage_turtle` | Event & Unlock Positioning | Thesis written (`agents/theses/sage_turtle_v1.md`); not yet in the active seed rotation. Monitors token unlock schedules, exchange listings, and macro events (unlock size vs. float, days-to-event window, recipient sell-propensity, pre-event funding/OI drift). Each event has idiosyncratic structure perfect for LLM reasoning at slow cadence with mechanical execution. Spawning it — alongside retiring `gray_finch`/`amber_wolf` — is an M8 "convert the desk" task, not yet done. |

**Retired (pending M8):** `gray_finch` (order book microstructure) and `amber_wolf` (trade flow) — microstructure at 5-min LLM cadence is structurally unwinnable; the slot is better spent on `sage_turtle`.

---

## Web Dashboard

Single web application at `localhost:8000`. Started automatically with `python forge.py`. Built with FastAPI (backend) + Jinja2 templates + vanilla JS + WebSocket (real-time P&L). No Node.js. No build pipeline. No external services.

### Pages

**/ — Desk Overview**
- Portfolio aggregate: total equity, MTD return, portfolio max DD, weighted Sharpe
- Active agent leaderboard: sortable by any metric (win rate, Sharpe, PF, return, drawdown); shows "vs null" column
- Live positions panel: all open positions across all agents, current P&L (updates via WebSocket)
- System health bar: exchange connectivity, LLM status, last wakeup times per agent

**/ agents/{name} — Agent Detail**
- Status badge (ROOKIE / ACTIVE / SUSPENDED / SHADOW / LIVE)
- Equity curve (SVG, updates live)
- Full performance stats (all-time + last 20 + last 7 days + by regime)
- Open positions with live P&L
- Trade history table: filterable, sortable, click to expand full fingerprint
- Thesis tab: current thesis + version history + diff view between versions
- Spec tab: current executable strategy spec + version history
- Reflection log: each reflection's evidence, research findings, proposed changes, adversarial critique, outcome
- Calibration report: per-agent confidence vs realized win-rate curve
- "Reflect Now" button: triggers reflection cycle immediately
- "Promote to Shadow" / "Go Live" buttons (with confirmation dialog)

**/graveyard — Terminated Agents**
- Grid of all terminated agents with final stats
- Click into any for full detail (read-only version of agent detail page)
- Shows termination reason, date, and best-performing trades

**/trades — Trade Bank**
- Full trade history across all agents (active and terminated)
- Filters: agent, asset, direction, outcome, regime, date range, thesis version
- Click to expand full fingerprint including OHLCV chart
- SQL query builder for ad-hoc analysis

**/decisions — Decision Log**
- Every heartbeat cycle: enter/wait/close/risk_blocked/error
- Counterfactual outcomes filled in nightly
- Enables calibration analysis and reflection loop training data

**/chat — Head of Desk**
- Chat interface with the Head of Desk LLM
- Head of Desk has full access to all agent data, all trade history, all thesis versions, graveyard
- Example queries: "Why is jade_hawk underperforming in ranging markets?", "What patterns are working best across the desk right now?", "Which agents are running similar theses?"
- Responses stream in real-time via WebSocket
- Chat history persisted in SQLite

**/settings — Configuration**
- Desk-wide settings (all editable, take effect on "Save"):
  - Number of active traders (target)
  - Maximum leverage per trade
  - Maximum position size (% of account)
  - Maximum notional exposure (size × leverage)
  - Wake cadence (minutes between agent wakeups)
  - Reflection trigger (every N trades / every N days / manual only)
  - Starting account balance for new agents
  - Universe assets (add/remove)
  - Evaluation thresholds (culling criteria)
- Emergency stop button: closes all open positions immediately (paper or live)
- Exchange connection status + reconnect button

---

## Tech Stack

```
Language:         Python 3.11+
Exchange / data:  Hyperliquid REST API (direct, no library required — clean REST)
Local inference:  Ollama + Qwen3.6-35B (compiled agent decisions, risk officer)
API inference:    Claude claude-sonnet-4-6 (reflection loop, head of desk synthesis)
Web research:     Brave Search API or SerpAPI (reflection cycles only)
Persistence:      SQLite (forge.db — local, disposable cache, no server) +
                  git-native ledger (ledger/ + state/ — committed/pushed every
                  heartbeat cycle, the actual system of record; see Data Layer & Ledger)
Backtest engine:  Replay history through spec interpreter + fee/slippage model (M7b, not yet built)
Web backend:      FastAPI + Jinja2 + WebSocket
Web frontend:     Vanilla HTML/CSS/JS (no build step, no npm)
Charts:           uPlot (lightweight, no dependencies, served as static file)
Scheduling:       APScheduler (in-process, no external scheduler)
Config:           YAML (desk-wide) + SQLite settings table (runtime-editable)
Strategy spec:    JSON/YAML DSL over heartbeat feature vocabulary
Entrypoint:       python forge.py (starts all agents + web server in one process)
```

---

## Repository Structure

```
forge/
├── forge.py                      ← single entrypoint: starts everything
├── config.yaml                   ← desk-wide defaults (no secrets)
├── .env.example                  ← required environment variables
├── requirements.txt
├── README.md
│
├── data/
│   ├── forge.db                  ← SQLite (gitignored — local, disposable cache,
│   │                                 rebuildable via scripts/rebuild_local_cache.py)
│   └── schema.sql                ← table definitions + migrations
│
├── ledger/                       ← git-tracked, append-only source of truth
│   ├── decisions/{YYYY-MM}.{jsonl,parquet}
│   ├── candles_5m/, candles_1h/, candles_4h/, funding/, oi/, liquidations/
│   ├── trades/, accounts/
│   └── events.jsonl              ← event calendar (not yet built, M7b)
│
├── state/
│   └── current.json              ← git-tracked, overwritten + committed every cycle
│                                     (open positions, live balances, agent status)
│
├── agents/
│   ├── runtime.py                ← agent async loop (wake → decide → execute)
│   ├── decision_loop.py          ← fetch market data → build prompt → call LLM → parse
│   ├── prompt_builder.py         ← assembles full decision prompt from all context
│   ├── reflection.py             ← thesis + spec update loop with all safeguards
│   ├── persona.py                ← system prompt constructor
│   └── theses/
│       ├── jade_hawk_v1.md       ← thesis versions committed to git
│       ├── jade_hawk_v2.md
│       └── ...
│
├── market/
│   ├── provider.py               ← MarketProvider facade; selects stub or hyperliquid via config
│   ├── stub.py                   ← StubMarket async class + get_market_state() (deterministic data)
│   ├── hyperliquid.py            ← HyperliquidClient: REST, circuit breaker, rate-limit retry
│   ├── heartbeat.py              ← generates the shared heartbeat packet; export_heartbeat_to_ledger()
│   │                                 writes lean candles/funding/oi/liquidations to ledger/ every cycle
│   ├── regime.py                 ← market regime classifier
│   ├── features.py               ← FEATURE_REGISTRY plugin pattern for derived indicators
│   └── web_research.py           ← search API client (reflection only)
│
├── risk/
│   └── gate.py                   ← stateless validator, non-bypassable (hardened)
│
├── execution/
│   ├── bridge.py                 ← TradingBridge ABC
│   ├── paper_bridge.py           ← simulate fills: price ± spread/2 ± slippage
│   └── live_bridge.py            ← real Hyperliquid order submission
│
├── backtest/
│   ├── interpreter.py            ← strategy spec DSL interpreter over heartbeat features
│   ├── engine.py                 ← replay history through interpreter + fee/slippage model
│   └── walk_forward.py           ← train/validate/test harness + overfit metrics
│
├── store/
│   ├── db.py                     ← SQLite connection + CRUD helpers
│   ├── fingerprint.py            ← write/query/update trade fingerprints
│   ├── performance.py            ← rolling metric calculation (daily equity Sharpe, etc.)
│   ├── positions.py              ← desk position registry; execute_close() ledger-exports
│   │                                 the closed trade + account snapshot on every close
│   ├── ledger.py                 ← append_ledger_record(): the core git-native ledger writer
│   ├── git_sync.py               ← best-effort commit + push of ledger/ + state/ every cycle
│   ├── state_snapshot.py         ← write_current_state(): state/current.json every cycle
│   ├── query.py                  ← structured query builder for trade bank
│   └── decisions.py              ← decisions table: every heartbeat cycle (also ledger-exported
│                                     via agents/decision_loop.py's log_decision)
│
├── meta/
│   ├── controller.py             ← evaluation loop + cull/spawn/graveyard
│   ├── evaluator.py              ← per-agent metric assessment vs null model
│   ├── spawner.py                ← new agent creation from seeds or harvested fingerprints
│   ├── risk_officer.py           ← mid loop: regime, exposure throttle, kill flags
│   └── head_of_desk.py           ← synthesis LLM: chat, analysis, spawn guidance
│
├── web/
│   ├── app.py                    ← FastAPI app, routes, WebSocket
│   ├── templates/
│   │   ├── base.html
│   │   ├── overview.html
│   │   ├── agent_detail.html
│   │   ├── graveyard.html
│   │   ├── trade_bank.html
│   │   ├── decisions.html
│   │   ├── chat.html
│   │   └── settings.html
│   └── static/
│       ├── forge.css
│       ├── forge.js
│       └── uplot.min.js
│
└── scripts/
    ├── fresh_start.py            ← wipe local + legacy data, seed all agents
    ├── compact_ledger.py         ← monthly JSONL→Parquet compaction + resolution decay
    ├── rebuild_local_cache.py    ← disaster recovery: rebuild forge.db from ledger/ + state/
    ├── spawn_agent.py            ← CLI: manually create a new agent (not yet built)
    └── promote_agent.py          ← CLI: move agent to shadow or live mode (not yet built)
```

Everything lives in one GitHub repo. No external services required beyond the exchange API and Ollama (local). `git clone` → `pip install -r requirements.txt` → configure `.env` → `python scripts/rebuild_local_cache.py` (reconstructs `data/forge.db` from the git-tracked `ledger/` + `state/current.json` — skip this on a genuinely fresh desk with no history yet) → `python forge.py` → system is running, exactly where it left off.

---

## Development Plan

Each milestone is independently demonstrable. You can start Forge after any milestone and observe meaningful behavior.

### M1 — Walking Skeleton (DONE)

**Goal:** The complete system structure exists. One agent wakes on a schedule, makes a trade decision using stub data and a stub LLM, records the fingerprint to SQLite, updates a paper account, and the web UI shows it happening. Nothing is real yet — but every seam in the architecture is proven.

**You can verify:** `python forge.py` → open `localhost:8000` → see one agent making stub trades every 60 seconds with records appearing in the UI.

**Tasks:**
1. Initialize git repository with full directory structure per the repo layout above; add `.gitignore` (exclude `.env`, `*.pyc`, `__pycache__`)
2. Write `config.yaml` with desk defaults: universe (15 assets), max leverage (10), max position size (0.20), wake interval (60s), starting balance (50000), target agent count (10)
3. Write `data/schema.sql` defining all SQLite tables: `agents`, `theses`, `trades`, `accounts`, `positions`, `reflections`, `evaluations`, `settings`, `chat_history`, `decisions`
4. Implement `store/db.py`: SQLite connection (WAL mode), schema initialization on first run, parameterized CRUD helpers for each table
5. Implement `market/stub.py`: returns hardcoded realistic OHLCV arrays, funding rates (-0.01 to +0.03), OI values, and liquidation volumes for all 15 assets — deterministic but plausible
6. Implement `risk/gate.py`: validates order dict (SL/TP geometry: long SL < entry < TP, short TP < entry < SL; TP non-null; entry within 0.5% of heartbeat price; R:R ≥ 0.5; notional exposure cap); raises `RiskViolation` with reason string on failure
7. Implement `execution/paper_bridge.py`: accepts validated order, simulates fill at stub mid price ± spread/2, writes trade to `trades` table with status='open', updates `positions` table, updates `accounts` table balance
8. Implement `agents/persona.py`: builds system prompt string from agent name + config + evaluation targets (as shown in The Trader-Agent section)
9. Implement `llm/stub.py`: returns a hardcoded valid trade decision JSON (action: "enter", SOL long, with all required fields) — no LLM call, purely for skeleton testing
10. Implement `agents/prompt_builder.py`: assembles decision prompt from thesis text + stub performance summary + empty trade history + empty positions + stub market state; returns string
11. Implement `agents/decision_loop.py`: fetch market data → build prompt → call LLM → parse JSON response → validate schema → pass to risk gate → execute via paper bridge → write fingerprint skeleton (outcome fields null)
12. Implement `agents/runtime.py`: async loop that wakes every N seconds (from config), calls `decision_loop`, catches and logs all exceptions without crashing
13. Implement `web/app.py`: FastAPI app with single route `GET /` returning Jinja2 template; template shows agent name, paper balance, and a table of the last 10 trades from SQLite
14. Implement `forge.py`: creates one stub agent (`jade_hawk`) in SQLite if not exists, starts APScheduler with agent runtime, starts uvicorn web server — all in one process
15. Seed `jade_hawk` initial thesis at `agents/theses/jade_hawk_v1.md` with the funding rate mean reversion seed hypothesis

**Done when:** `python forge.py` runs without error for 5 minutes, agent makes at least 3 stub trades, all appear in SQLite and web UI.

---

### M2 — Real Market Data (DONE)

**Goal:** Agents pull live market data from the Hyperliquid API. All 15 assets are priced in real time. The paper bridge simulates fills against real Hyperliquid bid/ask. The web UI shows live prices updating.

**You can verify:** Open the UI, see real BTC/ETH/SOL prices. Trigger a manual agent wake. See the trade recorded at a real Hyperliquid price. Data lag is <5 seconds.

**Tasks:**
1. Study Hyperliquid REST API docs and identify endpoints for: OHLCV candles, funding rates, open interest, recent liquidations, current order book — document base URLs and request format in `market/hyperliquid.py` header comments
2. Implement `market/hyperliquid.py`: `get_ohlcv(asset, interval, lookback_candles)`, `get_funding_rate(asset)`, `get_open_interest(asset)`, `get_liquidations(asset, hours=4)`, `get_orderbook(asset, depth=5)` — all using `httpx` async client
3. Implement rate limit handling in `market/hyperliquid.py`: exponential backoff on 429, circuit breaker after 5 consecutive failures (marks exchange as unavailable)
4. Implement `market/provider.py`: unified interface with `stub` and `hyperliquid` backends, selected by `config.yaml` flag `data_source`
5. Update `agents/prompt_builder.py` to pull real market state from provider (OHLCV + funding + OI + liquidations for all 15 assets)
6. Update `execution/paper_bridge.py` to fetch real Hyperliquid bid/ask at fill time and use mid-price ± spread/2 for paper fill simulation
7. Implement `market/regime.py`: derives market regime tag from BTC 30-day OHLCV (trend direction + ATR percentile) → returns one of five regime strings; add regime field to all new fingerprints
8. Add `/api/prices` WebSocket endpoint to `web/app.py`: broadcasts current prices for all 15 assets every 3 seconds using Hyperliquid order book
9. Update `web/templates/base.html` to include a live price ticker bar at the top (updates via WebSocket)
10. Add `/health` endpoint returning JSON: exchange connectivity status, last successful data fetch per asset, SQLite file size, uptime
11. Update web UI overview page to show "LIVE DATA ✓" or "STUB DATA ⚠" badge based on health check
12. Stress test: verify 15-asset data pull completes within agent wake budget (< 10 seconds total)

**Done when:** Agent wakes, fetches real SOL price, places paper trade at that price, recorded in SQLite. Web UI shows live prices updating without manual refresh.

---

### M3 — Real LLM Decisions (DONE)

**Goal:** Replace the stub LLM with a real local Qwen3.6-35B. One agent runs autonomously for 24+ hours making genuine trading decisions based on real market data. Every decision is logged with the agent's full reasoning text. Model is pinned; fallback only within same model.

**You can verify:** Read the agent's reasoning in the trade log. See it skip trades when the market doesn't fit its thesis. See it reference specific funding rate values and price levels from real data.

**Tasks:**
1. Add Ollama setup instructions to README: install Ollama, `ollama pull qwen3:35b`, verify with `ollama run qwen3:35b`
2. Implement `llm/ollama_client.py`: async POST to `localhost:11434/api/chat`, streams response, extracts JSON payload from response, handles timeout (30s hard limit → return None → agent logs "LLM timeout, skipping cycle")
3. Implement `llm/client.py`: unified interface dispatching to `stub` or `ollama` based on config flag; reads pinned model per agent from config
4. Implement `agents/prompt_builder.py` performance section: calculate and format all metrics from SQLite (win rate, PF, avg win/loss, Sharpe, by-regime breakdown) using `store/performance.py`
5. Implement `store/performance.py`: all metric calculations from raw trades table (win rate, profit factor, avg win, avg loss, daily equity Sharpe, max drawdown, by-regime breakdown)
6. Add last 10 closed trades section to decision prompt (query `trades` table, format each with asset, direction, PnL%, duration, hypothesis excerpt, outcome, postmortem)
7. Add current open positions section to decision prompt (query `positions` table for this agent)
8. Implement structured JSON response parser: validates all required fields exist in LLM output; if malformed, re-prompts with error message (max 2 retries before treating as "do nothing")
9. Implement "do nothing" path: LLM can return `{"action": "wait", "reason": "..."}` — logged to `decisions` table as a decision record (not a trade), included in next wake's context as recent activity
10. Implement "close early" path: LLM can return `{"action": "close", "position_id": "...", "reason": "..."}` — passes through risk gate minimum check, closes via paper bridge
11. Implement postmortem call: when a position closes (SL/TP hit or early close), make a second LLM call asking agent to write one-sentence postmortem; store in trade record
12. Log every decision with model name, temperature, and prompt hash
13. Run `jade_hawk` for 24 hours on real Hyperliquid data with real Qwen decisions; review: does the reasoning reference specific market conditions? Does it sometimes wait? Are stop losses always present?

**Done when:** 24h run produces 30+ trades with coherent reasoning text, some "wait" decisions, all risk gate rules observed, and every decision logged with model attribution.

---

### M4 — Trade Fingerprint Store (DONE)

**Goal:** Every trade is stored as a full fingerprint including OHLCV snapshot, funding context, regime tag, reasoning, and postmortem. Agents can query the historical bank. Cross-agent queries return results.

**You can verify:** Open the Trade Bank page, filter by asset and outcome, see win rates. Ask an agent (via its decision prompt) to reference a pattern from the trade bank — see it do so in its reasoning.

**Tasks:**
1. Update `trades` table schema: add columns for OHLCV snapshot (stored as msgpack blob), funding history array, OI data, liquidation data, regime tag, key_conditions_met/missing (JSON array), confidence (float), expected_value_text
2. Implement `store/fingerprint.py`: `write_entry(trade_dict)` captures market snapshot at entry time; `write_outcome(trade_id, outcome_dict)` updates on close; both use msgpack for OHLCV compression
3. Update `agents/decision_loop.py` to capture full market snapshot at the moment of entry decision and write complete fingerprint
4. Implement `store/query.py`: `query_trades(filters)` — filter by agent, asset, direction, regime, outcome, date range, funding_rate_range, oi_change_range; returns list of trade dicts (OHLCV decoded from msgpack)
5. Implement cross-agent query helper: `query_trades(agent_id=None)` returns trades from all agents; used in reflection and head-of-desk prompts
6. Add trade bank query section to agent decision prompt: before each decision, fetch the agent's own last 5 trades matching similar conditions (same asset OR same regime) and summarize their outcomes
7. Implement OHLCV chart renderer in `web/static/forge.js`: given a candles array, draws a simple SVG candlestick chart (no dependencies beyond vanilla JS)
8. Add `/trades` page to web dashboard: paginated table of all trades, filter controls (agent, asset, direction, outcome, regime), click to expand fingerprint detail with OHLCV chart
9. Add `/api/query` endpoint: accepts filter params, returns JSON trade list — enables the Head of Desk chat to query the bank via the API
10. Verify SQLite file size after 500 simulated trades with full OHLCV blobs is within acceptable range (< 50MB); adjust msgpack compression if needed

**Done when:** Trade bank page shows filterable history. Querying "SOL longs with negative funding" returns correct win rate. Agent decision prompt includes a cross-agent pattern reference.

---

### M5 — Multi-Agent Desk (DONE)

**Goal:** All 10 initial agents run simultaneously. Competing positions are visible and allowed — divergent theses in the same asset provide signal and natural desk hedging. The leaderboard shows all agents.

**You can verify:** Watch the leaderboard update live. Trigger two agents to want the same asset — confirm both can enter (same or opposing direction) and the desk position registry records both. Click into any agent to see their individual detail page.

**Tasks:**
1. Implement `store/positions.py`: `get_all_open_positions()` returns all open positions across all agents for desk-wide visibility
2. Remove the competing position check from `risk/gate.py`: competing positions are allowed — divergent theses in the same asset are signal and provide natural desk-wide hedging
3. Add "DESK POSITIONS" section to `agents/prompt_builder.py`: shows all other agents' current positions (formatted as in the design doc above)
4. Implement `meta/spawner.py`: `spawn_agent(name, seed_thesis_text, config_overrides)` — creates agent record in SQLite, writes thesis file, registers in scheduler
5. Run `scripts/fresh_start.py`: initialize all 10 agents with their seed theses; document in README
6. Update `forge.py` to launch all 10 agents as concurrent `asyncio` tasks; each agent's wake schedule is offset by 30s to avoid simultaneous Hyperliquid API bursts
7. Implement per-agent configurable wake interval: read from agent's config in SQLite (or fall back to desk default)
8. Add `/` overview leaderboard: table of all agents sorted by Sharpe (default), columns: name, status, trades, win%, PF, Sharpe, weekly return, max DD, BTC corr; sortable by clicking column header
9. Implement `/agents/{name}` page: equity curve (SVG via uPlot), full stats panel, trade history table, current thesis, open positions — all reading from SQLite
10. Add agent status badge (ROOKIE / ACTIVE / SUSPENDED / SHADOW / LIVE) with color coding
11. Add `/api/desk` endpoint: returns JSON summary of all agents' current state (for WebSocket broadcasts)
12. Broadcast desk state update via WebSocket every 30 seconds; update leaderboard table in-place without page reload
13. Test: manually trigger two agents to evaluate the same asset simultaneously; confirm both can enter positions (same or opposing direction) and the desk position registry records both correctly

**Done when:** 10 agents running simultaneously. Leaderboard updates live. Competing positions visible and correctly recorded across the desk. All agent detail pages accessible.

---

### M6 — Truth ("the numbers mean what they say") (DONE)

**Goal:** Fix measurement integrity. Today's results are contaminated by a risk-gate hole (wrong-side SL/TP fills booking phantom profits) and randomized model attribution. No learning can happen on corrupted signal.

**You can verify:** A week of runtime produces a leaderboard where every number is defensible, and the null agent's band is visible on it.

**Tasks:**
1. Harden `risk/gate.py`: SL/TP side validation (long: SL < entry < TP; short: TP < entry < SL), TP non-null and above fee hurdle, entry within 0.5% of heartbeat price, R:R ≥ 0.5, notional exposure cap (size × leverage); tests for each
2. Void the six corrupted trades; rebuild affected account curves; annotate in DB (never delete — graveyard ethos)
3. Realistic paper fills: spread + slippage-estimate applied by side; accrue funding on open positions in `update_position_pnl`; write `duration_minutes` on close
4. Pin one model per agent (config), retry-within-model only; log `model`, `temperature`, prompt hash on every decision
5. `decisions` table + nightly counterfactual filler for waits (what the entered-hypothetical would have returned at thesis-standard SL/TP)
6. Metrics rewrite: daily equity Sharpe/Sortino, capped PF (no `inf` with zero losses), exposure-adjusted returns; leaderboard shows "vs null"
7. Seed `random_walk` + `btc_hold` benchmark agents
8. Config hygiene: single source of truth for starting balance/universe; remove `forge.py`'s divergent seed list and duplicated main block

**Done when:** Every metric is defensible. The null agent's performance band is visible on the leaderboard.

---

### M7a — Git-Native Data Ledger (DONE)

**Goal:** Replace the gitignored, disposable `data/forge.db` as the system of record with a git-tracked, append-only ledger, so a fresh `git clone` reproduces the exact last-known state of the desk — the storage substrate M7b's backtester needs, built to satisfy the "everything fits in one git repo, no external database" constraint. Full design + implementation plan: `docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md`, `docs/superpowers/plans/2026-07-07-git-native-data-ledger.md`.

**You can verify:** `python scripts/rebuild_local_cache.py` on a machine with only `ledger/` + `state/current.json` present (no `data/forge.db`) reconstructs a working local cache with the same agents, balances, closed trades, and open positions the original machine had.

**What shipped:** the full Data Layer & Ledger design above — `store/ledger.py` (append-only JSONL writer, monthly-partitioned), lean market-data export from every heartbeat cycle (`market/heartbeat.py`, replacing the old verbose full-packet mirror), structured `confidence`/`evidence_strength` on **every** decision including "wait" (closing the selection-bias gap — see Anti-Overfitting §9, Calibration), trade-close + account-snapshot ledger export (`store/positions.py`), `state/current.json` written every cycle, best-effort git commit+push every heartbeat cycle (`store/git_sync.py`), monthly JSONL→Parquet compaction with resolution decay (`scripts/compact_ledger.py`), and the disaster-recovery rebuild script.

**Explicitly out of scope for M7a** (M7b, below): the strategy-spec DSL, the backtester itself, the liquidation feed. M7a builds the substrate; M7b builds what reads it.

---

### M7b — Strategy-Spec DSL & Backtest Engine (the strategic unlock — not yet built)

**Goal:** A backtest engine converts evolution from months-per-generation to minutes-per-generation. This is the single highest-leverage remaining build.

**You can verify:** `backtest(spec, 2025-07→2026-06)` returns an equity curve + overfit report in under a minute, and three seed specs have known historical profiles.

**Tasks:**
1. Fix z-score windows to match thesis definitions (14d funding baseline, etc.) — the ledger's `funding` stream now has real history to compute against
2. Event tables: funding settlements, macro calendar, **token unlocks** (`ledger/events.jsonl`, per Data Layer & Ledger above)
3. Strategy-spec DSL: schema + validator + interpreter over heartbeat features (entry conditions as weighted evidence terms, exit rules, sizing curve, regime filters, max hold, per-asset scope)
4. Backtester: replay history by reading `ledger/{kind}/*.{jsonl,parquet}` directly (or via a materialized local cache built the same way `rebuild_local_cache.py` does) through the same interpreter + fee/slippage model as paper; walk-forward harness (train/validate/test windows); overfit metrics (deflated Sharpe, parameter-sensitivity sweep)
5. Hand-compile 3 seed specs from existing theses (silver_basin, steel_crane, iron_moth) and backtest them — this is also the first honest evidence about whether these theses have any historical edge at all
6. Liquidation feed (WS flag or Coinalyze) replacing the proxy for steel_crane's family — lands in the ledger's existing `liquidations` stream
7. Statistical/Bayesian forecast feature (regime-conditioned empirical return distributions or a simple hierarchical/Bayesian regression by regime/asset), trained on the ledger's `candles_5m`/`funding`/`oi` streams via `scripts/build_training_dataset.py` (needs updating: it currently reads the retired `data/historical_data/*.jsonl` format — point it at the ledger instead). Output: expected forward return + credible interval, probability-of-up-move, injected as a new `FEATURE_REGISTRY` feature (`source=statistical`) alongside the existing `evidence_strength`/`confidence` fields agents already reason over. Explicitly NOT a deep-learning or LLM fine-tune — days-to-weeks of 5-minute-cadence data across 20 assets is too little and too noisy for a fine-tune to show real calibration gains; revisit only once this statistical feature has proven useful and history has grown to months, not days.

**Done when:** Backtest returns in under a minute. Three seed specs have known historical profiles. The statistical forecast feature is contributing to agent decisions via `FEATURE_REGISTRY`.

---

### M8 — Evolution (the actual product)

**Goal:** The reflection loop that turns Forge from a trading system into an evolutionary system — agents that produce and validate new strategies faster than old ones decay.

**You can verify:** An agent completes reflection → spec revision → backtest validation → hot deploy with zero human touches, and a rejected overfit revision is visible in the reflection log.

**Tasks:**
1. Reflection pipeline (frontier LLM): inputs = trade bank + decisions/counterfactuals + regime breakdown + backtest tools + (optional) web research; output = revised thesis + revised spec + self-declared invalidation conditions
2. Anti-overfit gates as *code*, wrapping the proposal's rules 1–7 around the backtester: min-trades, holdout, cross-agent validation, throttle, pattern persistence, adversarial pass (second LLM call attacking the spec), regime flags
3. Deploy pipeline: validated spec → versioned file + DB row → fast loop hot-reloads; full diff view in UI
4. Convert the desk: 6–7 compiled agents + 2–3 pure-LLM control-arm agents (temp 0, pinned local model). Retire gray_finch/amber_wolf; spawn event/unlock agent.
5. Calibration report: per-agent confidence vs realized win-rate curves (data already exists in `confidence` column)

**Done when:** End-to-end reflection cycle completes on a live agent. Rejected overfit revision visible in reflection log.

---

### M9 — Selection

**Goal:** The meta-controller that manages agent lifecycle with statistics that respect small samples.

**You can verify:** The desk runs 2+ weeks unattended: evaluated, culled, spawned, throttled — with a coherent morning summary in the chat.

**Tasks:**
1. Meta-controller evaluation job with statistics that respect small samples: compare each agent to the null distribution, probation before termination, evaluation cadence in *trades* not days
2. Cull/spawn/graveyard + harvest seeds; head-of-desk chat (frontier LLM with query tools over the bank) — daily briefing interface
3. Desk risk officer (mid loop): hourly regime memo, gross-exposure throttle, event blackout windows (no new entries 2h pre-FOMC etc.), kill-switch authority. Constraint: it can only *reduce* risk, never add.
4. Diversity maintenance: spawn thesis-similarity check (embedding or LLM compare against graveyard)

**Done when:** The desk runs 2+ weeks unattended with coherent morning summary.

---

### M10 — Live, Small

**Goal:** The first real-money trades — small, controlled, and only after clearing the null-model gauntlet.

**You can verify:** First agent in shadow mode with real orders visible on Hyperliquid. Paper vs live comparison report shows reasonable slippage numbers.

**Tasks:**
1. Live-bridge hardening: exchange-native trigger orders for SL/TP (never rely on the 5-min local loop for real stops), asset-specific size/price decimals (szDecimals), partial-fill and IOC-miss handling, position reconciliation against exchange state on every heartbeat
2. Shadow mode (paper + live simultaneously): slippage report, fill comparison
3. Promotion gate: ≥ 100 paper trades, beats null at 95%, positive after modeled costs, calibrated confidence, human click. Start with $500–1,000 on ONE agent (likely a funding-family agent).
4. Live audit log, webhook alerts, live emergency stop; daily automated paper-vs-live divergence report

---

### M11 — Compounding Operations (ongoing)

1. Capital allocation across live agents ∝ shrunk Sharpe (fractional-Kelly capped); weekly rebalance by meta-controller, human-approved.
2. Ops hardening: settings UI completion, restart recovery tests, DB backups, Docker, health metrics.
3. Research flywheel: monthly "strategy review" reflection at desk level — retire crowded edges, propose new families (this is where LLM creativity compounds into robustness-over-time).

---

### Historical note: heartbeat capture superseded by the git-native ledger

An earlier milestone ("Historical Heartbeat Data") built `append_historical()` (`market/heartbeat.py`) to mirror full heartbeat packets to `data/historical_data/*.jsonl`, and `scripts/build_training_dataset.py` to post-process them into a labeled Parquet dataset. Both shipped, but the capture mechanism was **retired and replaced** by M7a's git-native ledger: `append_historical()`/`HISTORICAL_DATA_DIR` no longer exist — `export_heartbeat_to_ledger()` writes the same underlying raw data (candles, funding, OI, liquidations), leaner and git-tracked, to `ledger/` instead of a gitignored local mirror. `scripts/build_training_dataset.py` still exists but needs a follow-up update to read from `ledger/` rather than the retired `data/historical_data/` format before it's usable again — tracked as M7b task 7 above, which also carries forward the still-relevant statistical-forecast-feature plan this milestone originally proposed.

---

## Known Risks

| Risk | Mitigation |
|---|---|
| Overfitting via the reflection loop | M7b's walk-forward + deflated-Sharpe machinery and the null-agent floor; anti-overfit gates are the most important code in the repo |
| All agents converge on same thesis | Competing positions provide signal when theses diverge; diversity spawns maintain breadth; Head of Desk flags thesis convergence |
| Regime shift breaks all agents simultaneously | Regime tagging surfaces regime-specific strategies; meta-controller suspends agents failing in new regime |
| Hyperliquid API downtime | Circuit breaker; agents skip cycle on unavailability; positions monitored by separate process |
| Qwen inference too slow for 15m cadence | 30s hard timeout; async execution; "do nothing" fallback preserves account safety |
| Live order submission failure | Paper bridge always succeeds; live bridge failure logs and alerts but does not corrupt paper state |
| Ledger grows too large for git | Resolved by design, not just mitigated: append-only JSONL (hot) → Parquet (cold, monthly compaction) keeps growth to ≈250MB/year at current scale; escalation ladder (tighter retention → resolution decay → shard-by-year → Git LFS) if that ever changes. See Data Layer & Ledger. |
| Market-history backfill incomplete | Backfill from Hyperliquid's API (candles + funding history reach back far enough); OI/liq accumulate live (not retroactively available from the exchange) |
| Big pickle / free-tier model unreliability | Pinned models per agent; frontier-model budget reserved for reflection where per-call value is 100× higher |
| Capacity limits at scale | These edges hold at $10k–$1M; not a problem worth solving yet |

---

## Honest Expectations

- **Calibrate the targets.** The original 25–45%/agent with Sharpe > 1.5 is aspirational; a realistic v1 success is: *funding-carry book yielding 10–25% annualized on small capital with max DD < 10%, plus optionality from event/cascade agents* — and, more importantly, a machine that produces and validates new strategies faster than old ones decay. That second thing is the actual asset being built.
- **Capacity is real but fine at this scale.** These edges hold at $10k–$1M; they will not hold at $100M. That is not a problem worth solving yet.
- **Biggest technical risk:** overfitting via the reflection loop — the system optimizing its specs into historical noise. Mitigation is M7b's walk-forward + deflated-Sharpe machinery and the null-agent floor; treat the anti-overfit gates as the most important code in the repo.
- **Biggest operational risk:** live stops living in a 5-minute local loop. Exchange-native triggers are non-negotiable before real money (M10.1).
- **Biggest strategic risk:** spending the next quarter polishing the fast loop (more features, more agents, more UI) instead of building M7b–M8. The fast loop is done enough. The moat is the slow loop.
- **Regulatory:** unchanged from the original proposal — non-custodial DeFi, US person, consult counsel before scaling live capital.

---

## Summary

Forge's ecosystem premise is right; its current LLM placement is one level too low. Fix measurement integrity first — today's results are contaminated by a risk-gate hole and randomized model attribution, and no learning can happen on corrupted signal. Reverse the no-history decision: a market-history store plus a backtest engine converts evolution from months-per-generation to minutes-per-generation, and it is the single highest-leverage build. Rebuild agents as LLM-authored, mechanically-executed, backtest-validated strategy specs — keeping a pure-LLM control arm so the arena itself answers the paradigm question. Concentrate the book on structural perp edges (funding, liquidations, events) where the market pays you for a mechanism rather than a prediction, and go live only through the null-model gauntlet, small.

*Forge is not a prediction machine. It is an evolutionary system that puts AI traders under genuine competitive pressure, gives them the tools to learn from the communal record of every trade ever made, and gets out of the way. The market decides who has edge. The desk decides who gets capital.*
