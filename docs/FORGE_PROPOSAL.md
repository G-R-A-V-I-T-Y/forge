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

**Additional data sources:**
- **Real liquidation feed** (Coinalyze — built, M7b): `market/coinalyze.py` lands in the existing `liquidations` ledger stream. History accumulates live only — never retroactively available.
- **Event calendar** (built, M7b): FOMC/CPI datetimes, **token unlock schedules** (predictable forced supply), listing announcements — `market/event_calendar.py`, landing in `ledger/events/{YYYY-MM}.jsonl` and exposed to specs as `days_to_event` / `unlock_size_pct`
- **Cross-exchange context** (not yet built — future): Binance/Bybit funding + basis for the same assets — dislocations between venues are cleaner mean-reversion signals than absolute levels

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
| `sage_turtle` | Event & Unlock Positioning | Spawned and compiled (M8) with spec `agents/specs/sage_turtle_v1.yaml` — short bias into large unlocks on a small-cap universe, `missing: veto` on event evidence (an event agent with no event data has nothing to trade on). Monitors token unlock schedules and macro events via `days_to_event` / `unlock_size_pct` replayable features. |

**Retired (M8, done):** `gray_finch` (order book microstructure) and `amber_wolf` (trade flow) — microstructure at 5-min LLM cadence is structurally unwinnable; the slot went to `sage_turtle`.

---

## Web Dashboard

Single web application at `localhost:8000`. Started automatically with `python forge.py`. Built with FastAPI (backend) + Jinja2 templates + vanilla JS + WebSocket (real-time P&L). No Node.js. No build pipeline. No external services.

The page inventory below is the functional spec. **M12 — Command Deck** rebuilds it as one modern, token-based design system with full executive actions (exit any trade, demote a trader, trigger reviews/reflections, live settings) — same lightweight stack, new coherence.

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
Backtest engine:  Replay ledger history through spec interpreter + fee/slippage model
                  with walk-forward + deflated-Sharpe overfit metrics (backtest/, M7b — built)
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
│   └── events/{YYYY-MM}.jsonl    ← event calendar: macro releases, token unlocks (M7b — built)
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
│   ├── dsl.py                    ← spec schema + YAML loader (frozen dataclasses)
│   ├── validator.py              ← semantic validation vs the replayable feature vocabulary
│   ├── interpreter.py            ← strategy spec DSL interpreter over heartbeat features
│   ├── engine.py                 ← replay ledger history through interpreter + fee/slippage model
│   └── walk_forward.py           ← train/validate/test harness + deflated Sharpe + param sweeps
│
├── store/
│   ├── db.py                     ← SQLite connection + CRUD helpers
│   ├── fingerprint.py            ← write/query/update trade fingerprints
│   ├── performance.py            ← rolling metric calculation (daily equity Sharpe, etc.)
│   ├── positions.py              ← desk position registry; execute_close() ledger-exports
│   │                                 the closed trade + account snapshot on every close
│   ├── specs.py                  ← spec deploy pipeline: versioned YAML + specs table + hot reload
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

## Owner's Review — 2026-07-09

M1–M8 are shipped (410 tests passing). The desk now has: a git-native ledger as system of record, a validated strategy-spec DSL with a walk-forward backtester, a reflection pipeline with anti-overfit gates as code, and a converted roster (4 compiled agents including `sage_turtle`, a pinned-model control arm, `gray_finch`/`amber_wolf` retired). The first honest seed backtests are in and show **no proven edge yet** — `iron_moth` is a textbook overfit signature, `silver_basin` barely fires, `jade_hawk` is "worth a closer look." That is the system working as designed: the machinery to find and validate edge exists; what remains is running it under selection pressure and making it operable.

Four owner priorities govern everything that remains:

1. **Traders get better every day.** The reflection pipeline exists but nothing schedules it. Until reflection and evaluation run on cadence, the desk is a static ensemble, not an evolutionary system. This is M9, and it is the highest-leverage remaining work.
2. **Data stays compact, meaningful, managed.** The ledger design is right (~250MB/year). Standing criteria checked at every milestone: monthly compaction runs and is verified; the `decisions` stream stays structured (no prose creep); the raw-not-derived discipline holds; no new gitignored shadow stores appear.
3. **Rigorous, calibrated trading.** Every agent is long/short capable with explicit sizing and leverage from its spec; calibration (stated confidence vs realized win rate) is a first-class per-agent metric and a hard promotion requirement; the null band is the floor under every claim of edge.
4. **One modern interface with line of sight and executive control.** The current UI is functional but fragmented. M10 rebuilds it as a **Command Deck**: portfolio → trader → trade drill-down, plus executive actions (exit any trade, demote/suspend a trader, trigger a review or reflection, edit live settings) — each confirmed, logged, and auditable.

Sequencing: **M9** (selection + cadence) → **M10** (Command Deck, partially parallel with M9) → **M11** (live, small) → **M12** (compounding operations). M10's design system and page shells can start while M9 is in flight; its executive actions wire up as M9 exposes them. *[Editorial note, 2026-07-13: milestone numbers in this dated review predate the plan revision below — Command Deck is now M12, Live is M13, Compounding is M14; the new M10/M11 are the Honest Reflection Engine and Population Learning. See the plan-revision note at the top of the Development Plan.]*

---

## Development Plan

Each milestone is independently demonstrable. You can start Forge after any milestone and observe meaningful behavior.

> **Plan revision — 2026-07-13.** A code audit of the reflection path found that the machinery M8 shipped cannot currently improve an agent: (a) the scheduled reflection wraps `llm/model_chain.py::decide()`, which validates every response as a *trade decision* (`action ∈ enter/wait/close`) — a model that correctly answers the reflection prompt with spec YAML is discarded, so every scheduled reflection ends "rejected" by construction; (b) the web "Reflect" action returns 503 because `forge.py` never sets `app.state.llm_fn`; (c) the overview's "Trigger All Evaluations" button posts to an endpoint that does not exist; (d) two anti-overfit gates (`check_holdout_split`, `check_cross_agent_validation`) are stubs that always pass; (e) the reflection prompt contains only aggregate stats — no per-trade fingerprints, no forward-labeled missed-trade evidence — and never revises the thesis.
>
> This revision (i) folds the transport repairs into M9, (ii) inserts two new milestones — **M10 Honest Reflection Engine** and **M11 Population Learning & Ecosystem Honesty** — that rebuild reflection around one principle: **the LLM proposes, the ledger disposes** (no LLM opinion ever decides a deploy; only replayed out-of-sample performance does), and (iii) renumbers the remaining milestones: Command Deck M10 → **M12**, Live M11 → **M13**, Compounding Operations M12 → **M14**. Read old numbers in material dated before 2026-07-13 (e.g. the 2026-07-09 Owner's Review) through that map.
>
> Design intent, tied to the owner's goals: each trader gets smarter (1) **the more it trades** — per-decision forward-labeling and hypothesis-driven reflection (M10); (2) **the more data the desk gathers** — every gate keys off the growing ledger, so validation sharpens automatically (M10); (3) **the more other agents trade** — validated/falsified knowledge is shared desk-wide and spawning recombines proven material (M11). And the system must never lock onto false positives: walk-forward replay is the only deploy authority, challenger trials confirm out-of-sample before promotion, and significance is deflated by desk-level trial accounting. What cannot be guaranteed is that an exploitable edge *exists* in the feature/DSL space; what is engineered is that if one exists the search finds and keeps it, and if none exists the desk says so instead of fooling itself.

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

### M7b — Strategy-Spec DSL & Backtest Engine (DONE)

**Goal:** A backtest engine converts evolution from months-per-generation to minutes-per-generation. This was the single highest-leverage build, and it shipped.

**What shipped:** `backtest/` in full — `dsl.py` (spec schema + YAML loader), `validator.py` (semantic validation against the replayable feature vocabulary), `interpreter.py` (weighted-evidence evaluator with veto logic), `engine.py` (ledger-replay through the exact same `compute_replayable_fields` live uses, with fee/slippage model), `walk_forward.py` (train/validate/test windows, deflated Sharpe, parameter-sensitivity sweeps). Z-score windows aligned to live's 14-day funding baseline. Event calendar (`market/event_calendar.py`, `ledger/events/{YYYY-MM}.jsonl`) wired into the heartbeat packet and into the replayable vocabulary — `days_to_event` and `unlock_size_pct` are registered features computable per historical bar with no lookahead. Coinalyze liquidation feed (`market/coinalyze.py`) lands in the `liquidations` stream. Statistical forecast feature (`statistical_forecast_return/vol/up_prob`) in `FEATURE_REGISTRY`. `scripts/build_training_dataset.py` reads from `ledger/`.

**First honest results** (`docs/superpowers/reports/2026-07-07-seed-backtest-results.md`): four seed specs exist — `silver_basin`, `iron_moth`, `jade_hawk` (swapped in for `steel_crane`, whose liquidation evidence can never be backfilled), and `sage_turtle`. `iron_moth`: real in-sample edge that fails out-of-sample (deflated Sharpe −2.74 — the exact overfit signature the harness exists to catch). `silver_basin`: fires rarely, thin sample, no edge shown. `jade_hawk`: mixed, test-window positive, "worth a closer look, not obviously dead." **No proven edge yet** — which is the honest baseline evolution starts from, not a failure of the machinery.

**Known limits (scoped, not hidden):** Hyperliquid's public candle endpoint serves ~208 days of 1h / ~17 days of 5m per asset — a depth ceiling, not a pagination bug. OI and liquidation history accumulate live only (never retroactively available). Walk-forward windows are correspondingly short until the ledger grows. Scaled-conviction sizing is not yet reflected in backtest P&L (tracked follow-up before any scaled-sizing deployment decision).

---

### M8 — Evolution (the actual product) (DONE)

> The reflection loop that turns Forge from a trading system into an evolutionary system — agents that produce and validate new strategies faster than old ones decay.

#### Goal
Build the end-to-end agent evolution pipeline: an agent running a backtested strategy spec (from M7b) completes a reflection cycle — reviewing its trade log, the decisions table with counterfactuals, and its backtest results — and autonomously produces a revised strategy spec. The new spec is validated with walk-forward backtesting, checked against anti-overfitting rules, and if it passes, hot-deployed to the fast loop. The agent's body then trades the new spec mechanically until the next reflection cycle.

**Status — shipped 2026-07-09.** `agents/reflection.py` (reflection prompt assembly, revised-spec parsing, every anti-overfit gate as code, adversarial second pass, calibration curve); `store/specs.py` deploy pipeline (versioned YAML under `agents/specs/`, `specs` table, hot reload via fresh `get_active_spec()` reads); compiled decision path in `agents/decision_loop.py` (`model = compiled/{spec_version}`); roster converted — compiled: `iron_moth`, `silver_basin`, `jade_hawk`, `sage_turtle` (specs deployed at seed/startup); control arm: `copper_vane`, `steel_crane`, `onyx_heron` (pinned model) plus `violet_lion`, `crimson_fox`; `gray_finch`/`amber_wolf` retired; real graveyard-similarity check in `meta/spawner.py`; spec diff view + calibration report on the agent detail page. All tests in the table below pass. **What M8 deliberately does not do:** schedule reflection — nothing triggers `run_reflection` on a cadence yet. That is M9's first acceptance criterion; until it lands, the end-to-end "done when" live cycle is pending runtime, not code.

#### Acceptance Criteria
1. A frontier LLM (Claude) is wired as the reflection engine: inputs include the agent's trade bank, decisions/counterfactuals, regime breakdown, backtest results from M7b, and (optionally) web research; output is a revised thesis markdown + a revised strategy spec YAML + self-declared invalidation conditions.
2. Anti-overfit gates (FORGE_PROPOSAL §Anti-Overfitting rules 1–7) execute as *code* wrapping the backtester — min-trade threshold, holdout split, cross-agent validation, update throttle, pattern persistence, adversarial second LLM pass attacking the revised spec, regime-flag awareness — and a rejected revision is logged in the reflection log with the specific gate that blocked it.
3. A deploy pipeline writes a validated spec to a versioned YAML file (named `{agent}_{spec_vN}.yaml` under `agents/specs/`), records a corresponding row in a new `specs` SQLite table, and hot-reloads the fast loop body so the next heartbeat executes the new spec. The web UI's agent detail page shows a diff view between spec versions.
4. The desk converts: 6–7 of the 10 agents become compiled agents (their decision loop calls `backtest/interpreter.py` with their active spec against the live heartbeat feature row instead of calling the LLM). 2–3 agents remain as a pure-LLM control arm (pinned local model, temperature 0, full decision logging including the `decision_details_json` that captures the agent's full reasoning). `gray_finch` and `amber_wolf` are retired; `sage_turtle` (event/unlock positioning) is spawned with a hand-compiled spec from M7b.
5. A calibration report is computed per-agent from the `confidence` column in the decisions table (data already exists) — a simple tabular or SVG plot of realized win rate per confidence bucket — visible on the agent detail page.
6. The `meta/spawner.py` `check_against_graveyard()` stub is replaced with a real thesis-similarity check that prevents spawning an agent with a thesis substantively similar to any terminated agent.

#### Dependencies
- **M7b must ship first**: the strategy-spec DSL (`backtest/dsl.py`, `backtest/validator.py`, `backtest/interpreter.py`) and backtest engine (`backtest/engine.py`, `backtest/walk_forward.py`) produce the outputs (validated specs, walk-forward reports) M8's reflection pipeline and anti-overfit gates consume. Without M7b, M8's reflection loop has nothing to produce, validate against, or compare.
- The ledger's `decisions` stream must have ≥7 days of "wait" decisions with counterfactuals filled in (M6's nightly job) so the calibration report and anti-overfit gate #3 (pattern persistence across non-overlapping 7-day windows) have real data.
- The feature vocabulary in `market/features.py` (`FEATURE_REGISTRY`) must include every term the first generation of compiled-agent specs references — the `funding_zscore`/`oi_zscore`/`atr_percentile`/`sector_relative_strength` features `silver_basin`/`steel_crane`/`iron_moth` specs require. (Feature names follow the same convention `backtest/dsl.py` validates against.)

#### Suggested Worktrees
| Worktree | Scope | Can Parallelise? |
|---|---|---|
| `m8-reflection-pipeline` | Reflection loop (frontier LLM call chain + anti-overfit gates + deploy pipeline) | After M7b done |
| `m8-compiled-agents` | Convert 6-7 agents to compiled mode; retire gray_finch/amber_wolf; spawn sage_turtle | After M7b + reflection pipeline |
| `m8-calibration-ui` | Calibration report rendering, spec diff view in web UI | Yes (independent UI work) |
| `m8-graveyard-similarity` | Real `check_against_graveyard()` using embedding or LLM compare | Yes (independent) |

#### Tests
| Test | What It Verifies |
|---|---|
| `tests/test_reflection.py` — `test_end_to_end_reflection_cycle()` | Reflection pipeline accepts trade bank + decisions + backtest results, returns a valid spec YAML, deploys it, agent's next decision uses the new spec |
| `tests/test_reflection.py` — `test_anti_overfit_rejects_overfit()` | A deliberately overfit spec revision (fits noise in the evidence window) is rejected by the holdout gate |
| `tests/test_reflection.py` — `test_anti_overfit_adversarial_pass()` | The second LLM call produces at least 1 invalidation condition; the spec is flagged if the adversarial pass finds a critical flaw |
| `tests/test_reflection.py` — `test_min_trade_gate()` | Reflection is skipped if the agent has fewer than 20 trades since the last update |
| `tests/test_reflection.py` — `test_update_throttle()` | A second reflection attempt within 30 trades or 14 days is blocked |
| `tests/test_reflection.py` — `test_pattern_persistence()` | A condition that appears in only 1 of 3 non-overlapping week windows fails the persistence gate |
| `tests/test_reflection.py` — `test_calibration_curve()` | Given synthetic decisions with known confidence/outcome pairs, the calibration report produces correct bucket-level win rates |
| `tests/test_spawner.py` — `test_graveyard_similarity_blocks_duplicate()` | Spawning an agent with a thesis substantively similar to a terminated agent is rejected |
| `tests/test_spec_deploy.py` — `test_spec_hot_reload()` | Writing a new spec version to the DB causes the agent's next heartbeat to use it without a restart |
| `tests/test_decision_loop.py` — `test_compiled_agent_uses_spec()` | A compiled agent (config flag `compiled: true`) calls the interpreter instead of the LLM, and the `model` field is set to `compiled/{spec_version}` |
| `tests/test_decision_loop.py` — `test_control_arm_uses_llm()` | A pure-LLM control-arm agent still calls the LLM even when a spec is present |

#### Done when
End-to-end reflection cycle completes on a live compiled agent: the agent has ≥20 post-reflection trades, triggers a reflection, produces a revised spec, the spec passes all anti-overfit gates, the pipeline deploys it hot, and the next heartbeat's decision uses `backtest/interpreter.py` (not the LLM). A rejected overfit revision is visible in the reflection log with the specific gate name that blocked it. The web UI shows both spec versions with a diff view and the calibration curve. `sage_turtle` is spawned and trading on its event/unlock spec.

---

### M9 — Selection & the Daily Improvement Loop

> The desk becomes self-regulating and self-improving on a schedule. This is the milestone that makes "traders getting better every day" literally true: reflection fires on cadence, evaluation fires on trade counts, losers face probation and the graveyard, and there's a Head of Desk you can interrogate.

#### Goal
Wire the improvement loop to clocks and counters: a meta-controller that evaluates every agent on a trade-count cadence against the null distribution (small-sample-aware), suspends/terminates/harvests per the lifecycle rules; a reflection scheduler that actually triggers M8's pipeline per the settings-table trigger (every N trades / every N days / manual); a mid-loop risk officer that can only reduce risk; and a Head of Desk LLM producing a daily briefing and answering natural-language questions over the full trade bank.

**Status (2026-07-13):** partially shipped. The evaluation cycle (`meta/controller.py::run_evaluation_cycle`, every 30 min) and the reflection scheduler (`meta/reflection_scheduler.py`, every 30 min) are wired in `forge.py`, and the per-agent web actions exist. Broken or pending: the reflection LLM transport (defect (a) in the plan-revision note — every scheduled reflection currently auto-rejects), `app.state.llm_fn` (defect (b) — the web Reflect button 503s), the trigger-all endpoints (defect (c)), risk-officer verification, Head of Desk (latched off in `forge.py` pending the R-track), and counterfactual-coverage surfacing. Criteria 2–3 below are the repairs.

#### Acceptance Criteria
1. **Reflection runs on cadence.** A scheduler job reads the reflection trigger from the settings table and, for each eligible agent, invokes `agents/reflection.py`'s pipeline **through the dedicated reflection transport (criterion 2), never through `llm/model_chain.py::decide()`**. Accepted revisions hot-deploy via `store/specs.py`; rejected revisions are logged with the specific gate that blocked them. The reflection log is queryable per agent. This closes M8's outstanding "done when": an end-to-end reflection cycle completes on a live compiled agent without human intervention.
2. **The reflection transport is real.** New `llm/reflection_client.py` exposing `complete(system_prompt: str, user_prompt: str, timeout_s: int = 900) -> str`: a raw-text completion call with **no decision-schema validation and no JSON coercion** — reflection output is spec YAML and thesis markdown, not a trade decision, and must reach the parser verbatim. Backend selected by a `reflection_model` settings key (same tier vocabulary as the model chain; default `llama_server` with reasoning ON — reflection is rare, so latency is acceptable; a Claude/opencode tier when configured). Every call logs model id and prompt hash. `forge.py`'s `_run_reflection_scheduler_job` passes this as `llm_fn` (deleting the current `mc_decide` closure), and `forge.py` sets `web_app.state.llm_fn` at startup so `POST /api/exec/trigger-reflection/{agent_id}` stops returning 503. Also fix the latent `deploy_spec(..., config.get("desk_config"))` call in `agents/reflection.py` to the `config["desk"]` convention (the `desk_config` key does not exist — see CLAUDE.md).
3. **Trigger-all endpoints exist.** `POST /api/exec/trigger-all-evaluations` runs `meta/controller.py::evaluate_agent` for every active/rookie agent with a new `force: bool = False` parameter that bypasses the interval-due check, writing one audit row per agent. `POST /api/exec/trigger-all-reflections` enqueues a reflection for every agent passing `check_agent_eligible` (via the criterion-2 transport) and returns per-agent `queued` / `skipped(reason)`. The overview's "Trigger All Evaluations" button wires to the former; a "Trigger All Reflections" button is added for the latter.
4. **Evaluation on trade cadence.** `meta/controller.py` + `meta/evaluator.py` run every N trades per agent (configurable, default 30): significance test against the null distribution from `benchmark_random_walk`'s trade history. An agent that does not beat the null at p < 0.05 after 50 trades enters probation; after 100 trades without improvement it is terminated. Cadence is measured in trades, not calendar days.
5. **Lifecycle rules enforced as code:** PF < 0.8 for two consecutive evaluations → suspend; max drawdown > 20% → immediate suspension; win rate < 35% after 50 trades → terminate; zero trades in 5 days → thesis review flag. Probation (SUSPENDED) before termination for borderline agents (p between 0.05–0.15, or PF 0.8–1.0); restore-or-terminate after 10 more trades or 7 days.
6. **Harvest & graveyard.** On termination, the agent's 5 best fingerprints (highest PnL%, cleanest thesis execution) are written to a `seeds` table for the next spawn generation. The full agent record persists permanently. `check_against_graveyard()` (already real from M8) is called on every spawn; rejections are logged to `evaluations`.
7. **Risk officer, reduce-only.** `meta/risk_officer.py` on a 30–60 min cadence: (a) regime memo from current market state, (b) aggregate gross exposure across all agents vs a desk-wide limit (default 2× total equity) with prorated entry-disable on the highest-exposure agents, (c) event-calendar blackout windows (no new entries 2h before FOMC/CPI), (d) kill flags on individual agent configs. **A validator asserts its output can never increase size, loosen a stop, or add entries.**
8. **Head of Desk.** `meta/head_of_desk.py`: a daily briefing job (desk P&L, regime alerts, agent-level divergence signals, pending human review actions) stored in `evaluations` and rendered on the overview page; a `/chat` WebSocket interface with query tools over trades/decisions/graveyard, streaming responses, history persisted in `chat_history`.
9. **Counterfactual coverage visible.** The nightly wait-counterfactual filler (M6) is verified running; its coverage (% of waits with filled counterfactuals) is surfaced on the decisions page so calibration and pattern-persistence gates are known to have real data. (M10 generalizes this filler into full decision forward-labeling; the coverage surface built here is reused.)

#### Dependencies
- **M8 (shipped):** reflection pipeline, spec deploy/hot-reload, compiled agents, calibration data.
- The `decisions` stream needs ≥7 days of waits with counterfactuals for the pattern-persistence and calibration gates to bite (accumulates during development).
- `evaluations` and `chat_history` tables exist in schema; verify and add a `seeds` table.

#### Suggested Worktrees
| Worktree | Scope | Can Parallelise? |
|---|---|---|
| `m9-reflection-cadence` | Scheduler wiring for the reflection trigger + reflection-log surfacing | Yes — start first; highest leverage |
| `m9-meta-controller` | Evaluation job, null-comparison statistics, probation/cull/harvest/graveyard | Yes |
| `m9-risk-officer` | Mid-loop memo, exposure throttle, event blackouts, reduce-only validator | Yes |
| `m9-head-of-desk` | Briefing job + chat with query tools + WebSocket streaming | Yes |

#### Tests
| Test | What It Verifies |
|---|---|
| `tests/test_reflection_schedule.py` — `test_trigger_fires_on_trade_count()` | With trigger "every 20 trades", an agent crossing 20 post-deploy trades gets a reflection run queued |
| `tests/test_reflection_schedule.py` — `test_rejected_revision_logged_with_gate()` | A gated rejection appears in the reflection log naming the blocking gate |
| `tests/test_reflection_schedule.py` — `test_accepted_revision_hot_deploys()` | An accepted revision's spec is active on the agent's next decision without restart |
| `tests/test_meta_controller.py` — `test_evaluation_runs_on_schedule()` | The meta-controller job fires at the configured trade interval and produces an `evaluations` row |
| `tests/test_meta_controller.py` — `test_cull_below_null()` | An agent underperforming the null at p < 0.05 after 100 trades is terminated |
| `tests/test_meta_controller.py` — `test_probation_suspension()` | An agent at PF 0.85 after 60 trades is SUSPENDED, not terminated |
| `tests/test_meta_controller.py` — `test_cull_on_drawdown()` | Agent with >20% max drawdown is immediately suspended |
| `tests/test_meta_controller.py` — `test_cull_on_win_rate()` | Agent with <35% win rate after 50 trades is terminated |
| `tests/test_meta_controller.py` — `test_zero_trade_review()` | Agent with 0 trades in 5 days triggers thesis review |
| `tests/test_head_of_desk.py` — `test_daily_briefing_produces_text()` | The briefing call returns a non-empty string referencing at least one agent by name |
| `tests/test_head_of_desk.py` — `test_chat_query_returns_stream()` | A `/chat` query returns a streaming WebSocket response referencing actual trade data |
| `tests/test_risk_officer.py` — `test_gross_exposure_throttle()` | With aggregate notional > 2× equity, entries are disabled on enough agents to bring exposure under threshold |
| `tests/test_risk_officer.py` — `test_event_blackout_blocks_entries()` | Within the 2h pre-FOMC window, new entries are blocked desk-wide (existing positions unaffected) |
| `tests/test_risk_officer.py` — `test_risk_officer_cannot_add_risk()` | Any output field that increases size, reduces SL distance, or adds entries fails validation |
| `tests/test_spawner.py` — `test_harvest_seeds_on_termination()` | On termination, the 5 best fingerprints are written to the `seeds` table |
| `tests/test_spawner.py` — `test_spawn_from_harvest()` | A new agent can be spawned from a harvested seed fingerprint |
| `tests/test_reflection_client.py` — `test_complete_returns_raw_text()` | The reflection transport returns the model's raw text; spec-YAML output is neither coerced into nor rejected as a trade decision |
| `tests/test_reflection_client.py` — `test_scheduler_uses_reflection_transport()` | `_run_reflection_scheduler_job` passes the reflection client, not `model_chain.decide`, as `llm_fn` |
| `tests/test_web_actions.py` — `test_trigger_all_evaluations()` | POST evaluates every active/rookie agent (force-bypassing the interval check) and writes one audit row per agent |
| `tests/test_web_actions.py` — `test_trigger_all_reflections_respects_eligibility()` | Ineligible agents are skipped with a reason; eligible agents get a reflection run via the transport |
| `tests/test_web_actions.py` — `test_reflect_endpoint_has_llm_fn()` | With forge's startup wiring, `/api/exec/trigger-reflection/{agent}` does not return 503 |

#### Done when
The desk runs 2+ weeks unattended: reflections fire per the configured trigger and at least one spec revision has been decided **on the merits** — its LLM output parsed as a spec and accepted-and-deployed or rejected-with-named-gate on a real agent, not lost in transport; evaluations run on trade cadence; an underperformer has been suspended or terminated with seeds harvested; the Head of Desk produces a coherent morning brief daily; the risk officer enforces exposure limits and blackouts; `/chat` answers questions about desk performance. All acceptance tests pass with synthetic data.

---

### M10 — Honest Reflection Engine (Diagnose → Propose → Validate)

> The reflection loop becomes a scientist, not an oracle: the LLM reads per-decision evidence and states falsifiable hypotheses; the ledger replay — and only the ledger replay — decides what deploys. **The LLM proposes; the ledger disposes.**

#### Goal
Rebuild `agents/reflection.py` from a single-shot "here are your aggregate stats, emit YAML" call into a three-stage pipeline over rich evidence: (A) **Diagnose** — a thinking-grade LLM reads an evidence dossier (per-trade fingerprints, calibration, forward-labeled decisions *including the trades not taken*, feature-conditioned statistics mined from the ledger) and produces explicit falsifiable hypotheses; (B) **Propose** — a thesis revision and spec revision tied to those hypotheses; (C) **Validate** — mandatory walk-forward replay against the ledger, then a live "challenger" trial before the spec becomes the agent's active strategy. Every hypothesis is registered and later marked validated or falsified by out-of-sample results, so each agent accumulates an honest track record of what it has learned. This is the "smarter the more it trades" axis, and it also deepens automatically as the ledger grows — the "smarter the more data" axis.

#### Acceptance Criteria
1. **Every decision is forward-labeled against the ledger.** New `meta/labeling.py` with a nightly APScheduler job `run_labeling_job(conn, ledger_dir)`: for every `decisions` row (enter, wait, and close — all agents including benchmarks) whose timestamp sits at least the longest horizon behind the ledger head, compute from `ledger/candles_5m/`: forward return at 1h / 4h / 24h, max run-up and max drawdown per horizon, the outcome of the chosen action, the best-available action among {enter-long, enter-short (thesis-standard SL/TP), wait}, and **regret** = best-available outcome − chosen outcome. Results go to a new `decision_labels` table in the local cache (`decision_id, horizon, fwd_return_pct, max_runup_pct, max_drawdown_pct, chosen_outcome_pct, best_action, best_outcome_pct, regret_pct, labeled_at`) — deliberately **not** a ledger stream: labels are derived data, recomputable from raw candles (the raw-not-derived discipline), and the table is rebuilt by re-running the job. The job is idempotent, leaves labels null across ledger gaps (never interpolates), absorbs the M6 wait-only counterfactual filler (the existing `counterfactual_*` columns keep being written for compatibility), and its coverage (% of labelable decisions labeled) renders on `/decisions`.
2. **Reflection consumes an evidence dossier, not aggregates.** New `agents/dossier.py`: `build_dossier(conn, agent_id, ledger_dir) -> Dossier` (frozen dataclass with a `to_prompt(max_chars)` renderer that truncates by priority, never mid-record). Contents: the agent's full current thesis text and active spec YAML; last ≤50 closed trades with entry-fingerprint summary (regime, funding z-score, OI change, `key_conditions_met/missing`, confidence) and postmortems; the calibration curve (`compute_calibration_curve` — computed today, fed to nothing); the top-10 highest-regret labeled decisions with their market context (the "trades it didn't make" evidence); win-rate/PF by regime; feature-conditioned statistics mined from the labeled dataset (`scripts/build_training_dataset.py` output, refreshed nightly) — for each feature in the spec vocabulary, bucketed forward-return and win-rate with sample counts; the agent's own hypothesis track record (criterion 6); and the desk-memory digest once M11 lands.
3. **Three-stage reflection replaces the single-shot prompt.** `run_reflection` becomes: **Stage A** `diagnose(dossier, llm_fn) -> list[Hypothesis]` — 1–5 hypotheses, each JSON with `claim`, `evidence_refs` (dossier item ids), `predicted_effect` (metric + direction + magnitude), `falsification_condition`. **Stage B** `propose(dossier, hypotheses, llm_fn) -> Proposal` — revised thesis markdown + revised spec YAML, each spec change annotated with the hypothesis id it serves. **Stage C** mechanical validation: parse → zero-evidence guard (kept) → **complexity budget** — at most `desk.max_evidence_terms` (default 4) evidence terms, and a spec adding terms beyond the incumbent's count must beat the incumbent's walk-forward deflated Sharpe (complexity must pay for itself) → **mandatory walk-forward** — `run_walk_forward` on `config["ledger_dir"]` (resolving to `ledger/`; a missing or too-short ledger is a *hard, logged failure* — the current silent `except: skip` path is removed) requiring deflated Sharpe > 0 and no parameter-sensitivity fragility flag. The always-pass stubs `check_holdout_split` and `check_cross_agent_validation` are **deleted** — real out-of-sample validation is the walk-forward test window plus the challenger trial (criterion 5). `check_min_trades` and `check_update_throttle` remain as pre-gates. The adversarial pass is **demoted from gate to advisory**: findings are stored in `reflections.adversarial_critique` and appended to the revised thesis under "Known weaknesses", but LLM opinion never blocks a deploy — only replay evidence does.
4. **Thesis and spec co-revise atomically.** An accepted proposal writes `agents/theses/{agent}_v{N+1}.md`, inserts the `theses` row, bumps `agents.current_thesis_version`, and deploys the spec (recording `thesis_version = N+1`) — all or nothing; a failure at any step rolls back the rest. The reflection row fills the columns unused since M8: `research_findings_json` (dossier digest), `proposed_changes` (hypotheses + thesis/spec diffs), `adversarial_critique`, `holdout_result` (walk-forward report summary).
5. **Challenger trial: accepted specs must win live before they rule.** `store/specs.py` gains status `'challenger'` (the `status` column is free-text; no migration needed). An accepted revision deploys as challenger, not active. Each heartbeat the compiled body evaluates **both** specs: the incumbent's decision executes; the challenger's decision is only logged (a `decisions` row with `challenger_spec_version` inside `decision_details_json`, never reaching the risk gate or bridge). Once the challenger has `desk.challenger_min_decisions` (default 20) labeled decisions or `desk.challenger_max_days` (default 7) elapse, a scheduler job compares mean labeled regret over the window: challenger wins → promoted to `active` (incumbent → `inactive`); loses → `rejected`. Either way the outcome lands in `reflections` and resolves the cycle's hypotheses (criterion 6). Fully automatic — paper capital only; live promotion keeps its own human gate (M13).
6. **Hypothesis registry.** New table `hypotheses` (`id, agent_id, reflection_id, claim, feature, direction, regime_context, predicted_effect, falsification_condition, status, effect_observed, created_at, resolved_at`; `status ∈ proposed | challenger | validated | falsified | inconclusive`). Stage A registers rows; the challenger trial resolves them (predicted effect realized → `validated`; falsification condition met or challenger rejected → `falsified`; window expired without signal → `inconclusive`, with `effect_observed` recorded in all cases). The dossier includes the agent's own registry history, so it cannot re-propose its own falsified ideas without addressing the falsification.
7. **Reflection uses a frontier/thinking model by economics, not habit.** Reflection fires roughly once per 20 trades per agent — per-call cost is negligible while per-call value is the highest in the system, so this is where the strongest available model belongs. The `reflection_model` settings key (M9) defaults to the local `llama_server` tier with reasoning ON; when a Claude API tier is configured it becomes the Stage A/B model. Both stages record model id and prompt hash in the reflection row.

#### Dependencies
- **M9 must ship first** (transport, cadence, trigger-all): M10 rebuilds what M9 schedules.
- `pyarrow` (already in `requirements.txt`) and a nightly scheduled `scripts/build_training_dataset.py` run for criterion 2's conditional statistics.
- ≥14 days of ledger candles for a meaningful walk-forward train/validate/test split; the challenger trial needs criterion 1's labeling job running.
- The R-track shared cost model (`feat/r5-cost-model`): replay, paper fills, and labeling must share one fee/funding/slippage model — evolution converges to whatever the fitness function rewards, so a too-cheap simulator is an ecosystem-level bug, not a detail.

#### Suggested Worktrees
| Worktree | Scope | Can Parallelise? |
|---|---|---|
| `m10-labeling` | `meta/labeling.py`, `decision_labels` table, coverage on /decisions | Yes — start first; everything downstream reads labels |
| `m10-dossier` | `agents/dossier.py` + conditional-stats miner over the training dataset | After labeling |
| `m10-reflection-engine` | Three-stage `run_reflection`, complexity budget, mandatory walk-forward, stub-gate deletion, atomic thesis+spec deploy | After dossier |
| `m10-challenger` | `'challenger'` status, dual-spec evaluation in the compiled body, resolution job | Parallel with dossier (touches `store/specs.py` + `agents/decision_loop.py`, not reflection) |
| `m10-hypothesis-registry` | `hypotheses` table + registration/resolution + agent-page rendering | With reflection-engine and challenger |

#### Tests
| Test | What It Verifies |
|---|---|
| `tests/test_labeling.py` — `test_labels_computed_from_ledger()` | Synthetic candles + a known decision produce correct forward returns, run-up/drawdown, and regret at every horizon |
| `tests/test_labeling.py` — `test_wait_regret_positive_when_entry_would_win()` | A wait before a clean up-move gets `best_action="enter_long"` and positive regret |
| `tests/test_labeling.py` — `test_labeling_idempotent_and_gap_safe()` | Re-running labels nothing twice; a ledger gap leaves labels null, not wrong |
| `tests/test_dossier.py` — `test_dossier_includes_top_regret_decisions()` | The 10 highest-regret decisions appear with their market context |
| `tests/test_dossier.py` — `test_dossier_respects_char_budget()` | `to_prompt(max_chars)` truncates by priority, never mid-record |
| `tests/test_reflection.py` — `test_diagnose_returns_falsifiable_hypotheses()` | Stage A output parses; each hypothesis has claim, evidence refs, predicted effect, and falsification condition |
| `tests/test_reflection.py` — `test_walk_forward_gate_is_mandatory()` | A proposal with no ledger available fails loudly; nothing deploys |
| `tests/test_reflection.py` — `test_complexity_budget_blocks_term_creep()` | A 5th evidence term (over the default budget of 4) that doesn't beat the incumbent's deflated Sharpe is rejected |
| `tests/test_reflection.py` — `test_adversarial_pass_is_advisory()` | A CRITICAL adversarial finding is recorded and appended to the thesis but does not block a walk-forward-passing spec |
| `tests/test_reflection.py` — `test_thesis_and_spec_deploy_atomically()` | A spec-deploy failure leaves the thesis version unchanged, and vice versa |
| `tests/test_challenger.py` — `test_challenger_logs_without_trading()` | A challenger spec's decisions are logged with `challenger_spec_version` and never reach the bridge |
| `tests/test_challenger.py` — `test_challenger_promotion_on_lower_regret()` | The challenger with lower mean labeled regret over the window becomes active; the incumbent goes inactive |
| `tests/test_challenger.py` — `test_challenger_rejection_resolves_hypotheses()` | A losing challenger marks its hypotheses falsified with observed effects |
| `tests/test_hypotheses.py` — `test_registry_roundtrip()` | proposed → challenger → validated/falsified transitions persist with timestamps and observed effects |

#### Done when
On a live compiled agent with ≥20 trades and ≥14 days of ledger: a scheduled reflection builds a dossier containing at least one forward-labeled missed trade, registers ≥1 falsifiable hypothesis, proposes a thesis+spec revision, and is decided by walk-forward on the merits; an accepted revision runs as challenger and auto-resolves within its window, updating the hypothesis registry; the stub gates no longer exist in the codebase; labeling coverage is ≥90% for decisions older than 24h; and the agent page shows the dossier digest, hypothesis outcomes, and thesis/spec diffs for the cycle.

---

### M11 — Population Learning & Ecosystem Honesty

> One agent's lesson becomes every agent's prior, spawning becomes crossover + immigration under diversity pressure, and every claim of edge survives desk-level multiple-comparisons accounting. This is the milestone that makes the *ecosystem* — not any single strategy — the thing that converges.

#### Goal
Make learning population-level: a shared desk memory of validated/falsified hypotheses that every reflection consumes and the graveyard check enforces; spawning that recombines proven seeds (crossover), injects fresh uncorrelated theses (immigration), and steers toward under-covered niches (diversity); and desk-level statistical honesty — a global trial ledger deflating every Sharpe by the whole desk's search effort, and a bootstrap null replacing the normal-approximation significance test. With N agents × M reflections × K hypotheses, spurious "edges" are statistically guaranteed to appear; this milestone is what kills them.

#### Acceptance Criteria
1. **Desk memory.** New `meta/desk_memory.py`: `get_desk_digest(conn, max_items=20) -> str` summarizing the desk-wide hypothesis registry — the strongest validated hypotheses and all falsified ones, each with agent, regime context, effect size, and sample size. Every M10 dossier embeds this digest, so agent B reflects with agent A's paid-for lessons in context — the literal mechanism for "each trader gets smarter as others trade." Rendered as an overview panel and served at `GET /api/desk-memory`.
2. **The graveyard extends into hypothesis space.** `meta/spawner.py::check_against_graveyard` additionally rejects any seed thesis or spec revision that re-encodes a *falsified* hypothesis — matched on (feature, direction, overlapping `regime_context`) — citing the falsifying registry row in the rejection reason. The existing thesis-token similarity check is retained for prose-level duplicates. Reflection Stage C calls the same check before walk-forward, so the desk never re-spends backtest budget on known-dead regions.
3. **Global trial accounting.** New `backtest_trials` table (`id, spec_hash, agent_id, ran_at, data_window_start, data_window_end, deflated_sharpe, outcome`); every `run_walk_forward` invocation inserts one row. `backtest/walk_forward.py::_deflated_sharpe` takes `n_trials` = 1 + the count of desk-wide trials in the trailing 90 days whose data windows overlap the candidate's — ten agents reflecting weekly automatically raise the bar for everyone, which is the discipline that stops the desk from buying 500 lottery tickets and celebrating the winner. Desk trial count and desk-level deflated Sharpe render on the overview.
4. **Bootstrap null.** `meta/evaluator.py::significance_test` replaces the `2/sqrt(N)` normal approximation with a resampling test: 1,000 bootstrap resamples (each of size = the agent's closed-trade count) of `benchmark_random_walk`'s per-trade returns build the null Sharpe distribution; the agent's empirical percentile is its p-value. The R12 insufficient-data latch (null < 30 trades → never cull on null comparison) is preserved. Lifecycle rules consume the bootstrap p-value unchanged.
5. **Population operators.** `meta/spawner.py` gains: **crossover** — `spawn_from_seeds(conn, seed_ids, llm_fn)`: an LLM synthesizes one thesis from ≥2 parents' harvested seeds (their reasoning + thesis excerpts); the compiled spec must pass the M10 walk-forward gate before the agent's first trade; used seeds are marked (`seeds.used`, `seeds.spawned_agent_id`). **Immigration** — of every 3 spawns, at least 1 must be a fresh thesis not derived from any seed (diversity insurance against premature convergence). **Niche steering** — `desk_diversity(conn)` computes pairwise Jaccard overlap of active specs' evidence-term feature sets plus per-(signal-family × sector) coverage; the spawner targets the least-covered niche. The diversity metric renders on the overview.
6. **Desk-level scoreboard.** Overview and the Head-of-Desk morning brief report: desk aggregate equity vs the null band, desk deflated Sharpe (criterion 3), hypothesis validation rate (validated ÷ resolved), and the diversity metric. These are the numbers the M13 promotion review reads — the honest long-run claim is "this desk beats its null after global deflation," never a single agent's story.

#### Dependencies
- **M10 must ship first:** the hypothesis registry, labeled decisions, and mandatory walk-forward are what desk memory, the extended graveyard, and trial accounting consume.
- M9's harvest path (`seeds` writes on termination — shipped in `meta/evaluator.py::harvest_best_trades`) feeds crossover.
- An LLM transport for synthesizing spawn theses: reuse M9's `llm/reflection_client.py`.

#### Suggested Worktrees
| Worktree | Scope | Can Parallelise? |
|---|---|---|
| `m11-desk-memory` | `meta/desk_memory.py`, dossier hook, overview panel, API | Yes |
| `m11-trial-accounting` | `backtest_trials` table, deflation rewire, overview stats | Yes |
| `m11-null-bootstrap` | Bootstrap `significance_test` + evaluator tests | Yes |
| `m11-population-ops` | Crossover, immigration, niche steering, diversity metric | After desk-memory (graveyard extension) |

#### Tests
| Test | What It Verifies |
|---|---|
| `tests/test_desk_memory.py` — `test_digest_includes_falsified_with_context()` | The digest lists falsified hypotheses with agent, regime, and effect size |
| `tests/test_desk_memory.py` — `test_dossier_embeds_desk_digest()` | An M10 dossier for agent B contains agent A's validated hypothesis |
| `tests/test_spawner.py` — `test_falsified_hypothesis_blocks_spawn()` | A seed thesis re-encoding a falsified (feature, direction, regime) is rejected citing the registry row |
| `tests/test_spawner.py` — `test_crossover_requires_two_parents_and_walk_forward()` | `spawn_from_seeds` with seeds from 2 parents produces an agent only after its spec passes walk-forward |
| `tests/test_spawner.py` — `test_immigration_quota()` | Of 3 consecutive spawns, at least one is seed-free |
| `tests/test_spawner.py` — `test_niche_steering_targets_gap()` | With momentum crowded and carry empty, the next spawn targets carry |
| `tests/test_walk_forward.py` — `test_global_trials_deflate_sharpe()` | The same raw Sharpe yields a strictly lower deflated Sharpe after 100 additional overlapping-window desk trials |
| `tests/test_evaluator.py` — `test_bootstrap_p_value_calibrated()` | An agent sampled from the null itself is rejected at ≈ the nominal rate over many synthetic runs |
| `tests/test_evaluator.py` — `test_bootstrap_respects_insufficient_data_latch()` | A null with <30 trades produces no cull decisions from null comparison |
| `tests/test_diversity.py` — `test_feature_overlap_metric()` | Known spec sets produce the expected Jaccard overlap values |

#### Done when
A falsified hypothesis recorded by one agent visibly blocks a matching spawn or revision for another, with the citation in the rejection log; a crossover child of two terminated parents' seeds is trading; the immigration quota holds over a synthetic spawn sequence; the overview shows desk deflated Sharpe (moving with the trial count), hypothesis validation rate, and the diversity metric; and lifecycle culls are driven by bootstrap p-values. All acceptance tests pass.

---

### M12 — Command Deck (fresh, modern UI)

> One modern interface that ties the whole desk together: portfolio → trader → trade line of sight, and executive actions — exit a trade, demote a trader, trigger a review — from the same screen you saw the problem on.

#### Goal
Rebuild the web UI as a coherent, modern command center while keeping the lightweight stack (FastAPI + Jinja2 + vanilla JS + uPlot; no npm, no build step — lightweight is a feature, not a compromise). Dark-first design system built on tokens; live data over the existing WebSocket; every executive action confirmable, logged, and auditable.

#### Acceptance Criteria
1. **Design system.** `forge.css` rebuilt on design tokens (color, spacing, type scale), dark-first, one visual language across every page. Every page migrated; no legacy ad-hoc styles remain. Responsive down to a laptop split-screen.
2. **Overview is a command center.** Portfolio equity, MTD return, and drawdown; leaderboard with the null band visible; all open positions with live P&L; the M9 morning-brief panel; a live activity feed of recent decisions (enter/wait/close/blocked); a system-health strip (exchange connectivity, LLM server, last heartbeat age, ledger sync status). The M10/M11 surfaces render here too: labeling coverage, the desk-memory digest (top validated/falsified hypotheses), desk deflated Sharpe with the global trial count, and the diversity metric.
3. **Executive actions** — each with a confirmation dialog showing full context, an optional reason note, an audit row written to `evaluations`, and immediate effect:
   - **Exit any open position** (routes through the agent's bridge close path — never bypasses the fingerprint/ledger export)
   - **Demote a trader** (ACTIVE → SUSPENDED), triggering an M9 review cycle; restore likewise
   - **Trigger reflection now** / **trigger evaluation now** for any agent
   - **Trigger all evaluations** / **trigger all reflections** desk-wide (the M9 endpoints)
   - **Enable/disable new entries** per agent (human-set risk flag)
   - **Emergency stop** — paper scope now; live scope extends it in M13
4. **Agent page consolidation.** Status badge, equity curve, stats (all-time / last-20 / by-regime), open positions, trade history, thesis + spec tabs with version diffs, reflection log, calibration curve — one coherent page, not bolted-on sections.
5. **Settings fully functional.** Every field on `/settings` reads and writes the settings table and demonstrably changes behavior: wake cadence, reflection trigger, evaluation thresholds, universe add/remove, risk caps.
6. **Action safety.** Every action endpoint is POST-only, requires the confirmation token, and has a test proving the action occurred and the audit row was written. All existing web tests still pass.

#### Dependencies
- M9 exposes the demote/review/evaluate/reflect actions the UI invokes. The design system and page shells can start in parallel with M9–M11; action wiring lands as M9 does.
- M10–M11 produce the new read surfaces (hypothesis registry, desk memory, labeling coverage, diversity metric); those panels wire up as M10/M11 land.
- Hard constraint: no new runtime dependencies (no npm, no build pipeline, no external services).

#### Suggested Worktrees
| Worktree | Scope | Can Parallelise? |
|---|---|---|
| `m12-design-system` | Tokens, base.html, forge.css rebuild, migration of all pages | Yes — start first; others build on it |
| `m12-command-overview` | Overview page: portfolio, leaderboard, positions, activity feed, health strip, M10/M11 panels | After design system lands |
| `m12-exec-actions` | Action endpoints + confirmation dialogs + audit trail | Parallel with overview; wires to M9 |
| `m12-agent-page` | Agent detail consolidation (incl. dossier digest + hypothesis outcomes) | Parallel |

#### Tests
| Test | What It Verifies |
|---|---|
| `tests/test_web_actions.py` — `test_close_position_action()` | POST closes the position via the bridge path, writes fingerprint + audit row |
| `tests/test_web_actions.py` — `test_demote_triggers_review()` | Demote sets SUSPENDED and creates a review-pending `evaluations` row |
| `tests/test_web_actions.py` — `test_reflect_now_queues_reflection()` | The reflect-now endpoint queues an M9 reflection run for the agent |
| `tests/test_web_actions.py` — `test_actions_require_post_and_confirm()` | GET on any action endpoint is rejected; POST without the confirm token is rejected |
| `tests/test_web_actions.py` — `test_entry_disable_blocks_next_entry()` | With entries disabled via the UI flag, the agent's next enter decision is blocked and logged |
| `tests/test_web_settings.py` — `test_settings_roundtrip_changes_behavior()` | Editing wake cadence via the UI changes the scheduler's next interval |
| `tests/test_web_desk.py` — (existing suite) | Overview renders leaderboard with null band; no regressions across migrated pages |

#### Done when
Every page shares the design system. From the overview you can spot a problem (losing trader, stuck position, stale heartbeat) and act on it (exit, demote, trigger review) in under three clicks, with each action confirmed and auditable. Settings changes take effect without a restart. All action and rendering tests pass.

---

### M13 — Live, Small

> The first real-money trades — small, controlled, and only after clearing the null-model gauntlet. This is the milestone where the entire system justifies its existence: does the edge that survived paper trading survive the transition to real execution?

#### Goal
Harden the live bridge, implement shadow mode (paper + live simultaneously for the same agent), establish the promotion gate (statistical + human), and execute the first real-money trades on Hyperliquid with $500–1,000 on a single agent. Paper and live fills are compared in a daily divergence report; if execution quality is materially worse than paper, the agent is demoted back to paper-only until the gap is understood.

#### Acceptance Criteria
1. **Live bridge hardened (`execution/live_bridge.py`):**
   - Exchange-native trigger orders for SL/TP submitted at entry — never rely on the 5-minute local loop for real stops; a power loss must not orphan a position
   - Asset-specific decimal handling: `szDecimals`/`pxDecimals` read from the exchange info endpoint at startup; all order params rounded accordingly
   - Partial-fill handling: SL/TP sized to the filled quantity; unfilled remainder cancelled (IOC) or left working (GTC) per order type
   - IOC-miss handling: zero-fill returns `status="no_fill"`, no phantom position, cycle skipped gracefully
   - Position reconciliation every heartbeat: exchange is authoritative; any mismatch logs a reconciliation entry and corrects local state
   - `live_trades` table: immutable, append-only audit record of every real-money fill
2. **Shadow mode:** `config_json.shadow = true` runs both bridges in parallel per decision; positions tracked independently (`bridge='paper'` / `bridge='live'`), each validated by the risk gate under its own rules; `execution/shadow_reporter.py` writes a comparison (entry/exit price diff, slippage vs estimate, fill timing, one-sentence assessment) to a `shadow_comparisons` table at every live close; a daily 7-day divergence report recommends promote/continue/demote; any single trade with slippage > 0.3% raises an alert, three in a row locks new live entries pending a human click.
3. **Promotion gate:** statistical — ≥100 paper trades, beats null at 95% confidence (M11's bootstrap test), edge still positive under M11's desk-level trial deflation, positive return after modeled costs, calibration gap < 10 percentage points in every bucket with ≥10 decisions; human — a "Promote" action in the Command Deck with the full performance summary, shadow stats, the agent's hypothesis validation record (M10–M11), and a required review-note field, logged to `evaluations`. One agent only for the first promotion — the smoothest regime-adjusted equity curve (likely funding-carry), not the highest return. Capital fixed at $500–1,000 for ≥30 trading days before any increase.
4. **Audit & safety:** JSONL live audit log at `data/live_audit/{YYYY-MM-DD}.jsonl` (gitignored, never deleted) for every live event; webhook alerts (`alert.webhooks` in config.yaml) on fills, reconciliation mismatches, bridge errors, divergence > 0.3%, live drawdown > 5%; the Command Deck's EMERGENCY STOP extends to live scope — closes all exchange positions via IOC, disables all live bridges, requires a deliberate human "Restart Live"; the daily divergence report renders on the overview.

#### Dependencies
- **M9–M11 must ship first:** an agent that hasn't survived selection pressure, honest reflection (M10), and desk-level deflation (M11) has no business near real capital; the risk officer's exposure logic is extended for live rules.
- **M12 must ship first (or in final integration):** the promotion dialog, emergency stop, and divergence surfacing are Command Deck features.
- **Calibration report (M8 — shipped)** is a promotion-gate input.
- **$500–1,000 funded Hyperliquid wallet**, private key in `.env` (never config.yaml), verified with a $10 test order before any bridge runs.
- **Hyperliquid conditional-order support** at the account tier in use — if unavailable, acceptance criterion 1's exchange-native triggers are replaced by a documented redundant watchdog (separate process/timer polling every 60s), and the promotion decision accounts for the weaker guarantee.

#### Suggested Worktrees
| Worktree | Scope | Can Parallelise? |
|---|---|---|
| `m13-wallet-setup` | Wallet funding, `.env` config, $10 test order, API readiness verification | Must run first (prerequisite) |
| `m13-live-bridge` | Order submission, decimals, partial fills, IOC-miss, reconciliation, native triggers | Yes (core engineering) |
| `m13-shadow-mode` | Dual-bridge routing, `shadow_comparisons`, divergence reporter | After live bridge |
| `m13-promotion-gate` | Statistical checks + Command Deck promotion dialog + logging | Yes (independent of bridge) |
| `m13-audit-safety` | Audit log, webhooks, live emergency stop, daily report | Yes (independent of bridge) |

#### Tests
| Test | What It Verifies |
|---|---|
| `tests/test_live_bridge.py` — `test_submit_order()` (mock exchange) | A valid order dict translates to the correct Hyperliquid payload and returns a fill |
| `tests/test_live_bridge.py` — `test_sz_decimals_rounding()` | For `szDecimals=4`, a computed size of `0.12345` rounds to `0.1234` |
| `tests/test_live_bridge.py` — `test_partial_fill_handles_sl_tp()` | A 50% partial fill creates a half-size position with valid SL/TP for that half |
| `tests/test_live_bridge.py` — `test_ioc_miss_returns_no_fill()` | Zero-fill IOC returns `status="no_fill"`, no position row created |
| `tests/test_live_bridge.py` — `test_position_reconciliation_overwrites_local()` | An exchange position missing locally overwrites local state to match exchange |
| `tests/test_live_bridge.py` — `test_exchange_trigger_sl_tp_submitted()` | Entry submits an exchange-native conditional SL/TP alongside the order |
| `tests/test_shadow.py` — `test_shadow_dual_execution()` | Shadow flag causes both bridges to run and two positions rows to be created |
| `tests/test_shadow.py` — `test_shadow_violation_independent()` | An order passing paper rules but violating a live rule is rejected for live only |
| `tests/test_shadow.py` — `test_divergence_report()` | Known synthetic price differences produce correct slippage stats |
| `tests/test_emergency_stop.py` — `test_emergency_stop_closes_all()` | The stop endpoint closes all exchange positions and disables live bridges |
| `tests/test_emergency_stop.py` — `test_emergency_stop_requires_human()` | "Restart Live" requires explicit human confirmation via the UI |
| `tests/test_promotion_gate.py` — `test_promotion_requires_100_trades()` | 99 trades cannot be promoted |
| `tests/test_promotion_gate.py` — `test_promotion_requires_beats_null()` | Not beating the null at 95% blocks promotion |
| `tests/test_promotion_gate.py` — `test_promotion_requires_calibrated_confidence()` | A 20pp confidence-vs-win-rate gap in any bucket blocks promotion |
| `tests/test_promotion_gate.py` — `test_promotion_requires_human_reason()` | The promotion endpoint rejects a request without a review note |
| `tests/test_live_audit.py` — `test_audit_log_writes_on_every_event()` | Each live bridge event appends a line to the audit JSONL |
| `tests/test_live_audit.py` — `test_webhook_fires_on_live_fill()` | A live fill POSTs to the configured webhook |
| `tests/test_live_audit.py` — `test_daily_divergence_report_generates()` | The daily report produces correct aggregate stats |

#### Done when
A single agent has been promoted through the full pipeline: ≥100 paper trades → statistical + calibration checks → human review → shadow mode with an acceptable 7-day divergence report → full live on $500–1,000. The emergency stop exists and works. All acceptance tests pass. The daily divergence report shows live fills within 0.2% of paper fills on average.

---

### M14 — Compounding Operations (ongoing)

> The desk graduates from a prototype to an operation: capital follows risk-adjusted performance, the system survives unattended weeks, and the research flywheel produces new strategy families faster than old ones decay.

#### Goal
Scale from one live agent to several with fractional-Kelly capital allocation; harden operations for unattended running (restart recovery, backups, health metrics) without abandoning the lightweight single-process design; institutionalize a monthly desk-level strategy review that feeds the spawn queue.

#### Acceptance Criteria
1. **Capital allocation (`meta/capital_allocation.py`):** fractional Kelly per live agent (default fudge factor 0.25) from combined paper + shadow history (shadow trades weighted 0.3×); negative/zero Kelly → capital returned to desk reserve and agent demoted to paper-only; weekly Monday rebalance proposal written to `evaluations` and the morning brief, requiring human approval within 48h (else status quo persists); total live capital ≤ 50% of wallet; no agent > 25% of total live capital.
2. **Ops hardening (lightweight-first):**
   - Restart recovery proven: kill the process mid-operation, restart, and an integration test verifies open positions, balances, and agent statuses are identical (the M7a ledger/state rebuild path, exercised end-to-end)
   - Daily timestamped backup of `ledger/`, `state/`, and `data/forge.db` via APScheduler
   - `/health` extended: per-agent heartbeat age, counterfactual-job age, LLM latency p50/p95, Hyperliquid API error rate, DB size, ledger growth rate (MB/day); log rotation with 30-day retention
   - Docker + docker-compose are documented options for remote/unattended hosting (`DOCKER.md`), **not** a gate — the single-process `python forge.py` design remains primary
3. **Research flywheel:** monthly Head-of-Desk strategy review (cron `day=1 06:00 UTC`) over cumulative PnL/Sharpe by strategy family, agent PnL-correlation adjacency, graveyard patterns, the hypothesis registry (validation rate by strategy family — which families are *learning* vs. churning), and spec staleness; produces `data/monthly_reviews/{YYYY-MM}.md` with **Retire / Maintain / Seed** sections, also posted to `chat_history`; the Seed section becomes a spawn queue checked before default spawn logic whenever a slot opens; a per-family correlation metric tracked monthly — 3 consecutive rising months flags the family for forced de-correlation (size reduction + thesis divergence on next reflection).

#### Dependencies
- **M13 must ship first:** capital allocation needs at least one live agent with real history.
- **M9 and M11 must ship first:** the spawn mechanism and population operators (crossover, immigration, niche steering) are what the flywheel's Seed queue feeds.
- **≥3 months of trading data** for meaningful Kelly estimation (8-week trailing windows); before that, allocation falls back to equal-weight.
- Docker optional; if unavailable, ops criteria are verified with equivalent manual steps and documented.

#### Suggested Worktrees
| Worktree | Scope | Can Parallelise? |
|---|---|---|
| `m14-capital-allocation` | Kelly engine, weekly rebalance job, proposal + approval flow | After M13's first live agent exists |
| `m14-ops-hardening` | Restart-recovery test, backups, /health extension, log rotation, DOCKER.md | Yes (independent; can start during M13) |
| `m14-research-flywheel` | Monthly review prompt + report, seed injection into spawn queue, correlation tracking | After M9/M11 (needs spawn mechanism + population ops) |

#### Tests
| Test | What It Verifies |
|---|---|
| `tests/test_capital_allocation.py` — `test_kelly_fraction_computed()` | Known mean/variance produces the correct fractional Kelly |
| `tests/test_capital_allocation.py` — `test_negative_allocation_demotes()` | Negative optimal Kelly demotes the agent to paper-only |
| `tests/test_capital_allocation.py` — `test_weekly_rebalance_proposal()` | The rebalance job produces non-negative fractions within the total-live cap |
| `tests/test_capital_allocation.py` — `test_no_single_agent_over_25pct()` | No proposed allocation exceeds 25% of live capital |
| `tests/test_capital_allocation.py` — `test_rebalance_requires_human_approval()` | A new allocation is visible but not applied until approved |
| `tests/test_ops.py` — `test_restart_recovery()` (integration) | Process kill + restart preserves positions, balances, and history exactly |
| `tests/test_health.py` — `test_health_extended_metrics()` | `/health` returns all extended gauges with valid values |
| `tests/test_logging.py` — `test_log_rotation()` | Logs rotate on schedule and old files are cleaned up |
| `tests/test_research_flywheel.py` — `test_monthly_review_generates()` | The review job produces a markdown file with Retire/Maintain/Seed sections |
| `tests/test_research_flywheel.py` — `test_seed_injected_into_spawn_queue()` | After a review with a Seed entry, the next spawn uses the recommended thesis |
| `tests/test_research_flywheel.py` — `test_correlation_rising_triggers_action()` | 3 months of rising intra-family correlation flags forced thesis refinement |

#### Done when
Multiple live agents run with Kelly-proportional capital unattended for 4+ weeks; the system survives a hard restart with zero data loss; the monthly review has produced at least one Seed recommendation that was actually spawned; all acceptance tests pass.

---

### Historical note: heartbeat capture superseded by the git-native ledger

An earlier milestone ("Historical Heartbeat Data") built `append_historical()` (`market/heartbeat.py`) to mirror full heartbeat packets to `data/historical_data/*.jsonl`, and `scripts/build_training_dataset.py` to post-process them into a labeled Parquet dataset. Both shipped, but the capture mechanism was **retired and replaced** by M7a's git-native ledger: `append_historical()`/`HISTORICAL_DATA_DIR` no longer exist — `export_heartbeat_to_ledger()` writes the same underlying raw data (candles, funding, OI, liquidations), leaner and git-tracked, to `ledger/` instead of a gitignored local mirror. `scripts/build_training_dataset.py` was updated in M7b to read from `ledger/` (the legacy `data/historical_data/` mirror is gitignored and inert), and the statistical-forecast-feature plan this milestone originally proposed shipped as M7b's `statistical_forecast` feature.

---

## Known Risks

| Risk | Mitigation |
|---|---|
| Overfitting via the reflection loop | M7b's walk-forward + deflated-Sharpe machinery and the null-agent floor; M10 makes walk-forward the mandatory deploy authority with challenger trials confirming out-of-sample; M11 deflates every Sharpe by desk-wide trial count; anti-overfit gates are the most important code in the repo |
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
- **Biggest operational risk:** live stops living in a 5-minute local loop. Exchange-native triggers are non-negotiable before real money (M13, acceptance criterion 1).
- **Biggest strategic risk:** M7b–M8 are built, so the risk has moved. It is now spending the next quarter admiring the machinery instead of running it: until M9's reflection cadence and selection pressure fire daily, the desk is a static ensemble wearing an evolutionary system's architecture. The moat compounds only under load.
- **Regulatory:** unchanged from the original proposal — non-custodial DeFi, US person, consult counsel before scaling live capital.

---

## Summary

The foundation is built and honest: measurement integrity fixed (M6), a git-native history that fits in one repo (M7a), a backtest engine that turns evolution from months-per-generation to minutes-per-generation (M7b), and agents rebuilt as LLM-authored, mechanically-executed, backtest-validated strategy specs with a pure-LLM control arm so the arena itself answers the paradigm question (M8). The first backtests say what an honest system should: no proven edge yet. What remains is the part that makes it an ecosystem rather than an artifact — selection pressure and reflection running on a daily cadence with a working transport (M9), a reflection engine where the LLM proposes falsifiable hypotheses and the ledger disposes (M10), population-level learning under desk-level statistical honesty (M11), a Command Deck that gives its owner line of sight and executive control (M12), and only then the null-model gauntlet to live capital, small (M13), compounding into an operation (M14). Concentrate the book on structural perp edges — funding, liquidations, events — where the market pays for a mechanism rather than a prediction.

*Forge is not a prediction machine. It is an evolutionary system that puts AI traders under genuine competitive pressure, gives them the tools to learn from the communal record of every trade ever made, and gets out of the way. The market decides who has edge. The desk decides who gets capital.*
