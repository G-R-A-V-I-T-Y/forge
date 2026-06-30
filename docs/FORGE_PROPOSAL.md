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

Agents are not rule engines following a lookup table. They reason. A good Slay the Spire player doesn't say "rule: take every relic that gives block." They look at their current deck, HP, floor, and what boss is coming, then make a judgment call about what gives the best probability-weighted path to the top. Forge agents do the same: the thesis provides strategic identity and vocabulary; the LLM provides the reasoning; the performance data keeps it honest.

A rule says "enter when funding < -0.03%." A reasoning agent says "funding is -0.03% but OI has been falling for 12 hours, suggesting shorts are already covering — the setup is weaker than the number implies. Wait."

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

**15 assets (updated quarterly by Head of Desk):**

BTC, ETH, SOL, BNB, XRP, DOGE, AVAX, LINK, ARB, OP, SUI, TON, PEPE, WIF, + one rotating slot

Large enough to support dozens of distinct, non-overlapping strategies. Small enough to monitor quality signals across all assets without meaningful API cost.

---

## Target Performance

| Metric | Individual Agent Target | Portfolio Target (ensemble) |
|---|---|---|
| Annual return | 25–45% | 20–35% |
| Max drawdown | 8–15% | 3–6% |
| Win rate | >55% | — |
| Profit factor | >1.4 | — |
| Sharpe ratio | >1.5 | >2.0 |
| Trade frequency | 3–15 per day | — |

The portfolio drawdown is structurally lower than any individual agent's drawdown because agent equity curves are weakly correlated with each other and with BTC. Sizing live allocations in proportion to Sharpe ratio suppresses portfolio drawdown further.

The target numbers are aspirational, not engineering requirements. The system finds whatever edge exists and compounds it.

---

## System Architecture

```
                    HYPERLIQUID API
                  (market data, REST)
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
     ┌─────────┐   ┌─────────┐   ┌─────────┐
     │ Agent A │   │ Agent B │   │ Agent N │
     │ thesis  │   │ thesis  │   │ thesis  │
     │ account │   │ account │   │ account │
     └────┬────┘   └────┬────┘   └────┬────┘
          └──────────────┼─────────────┘
                         │ trade decisions
                         ▼
                  ┌──────────────┐
                  │  RISK GATE   │  ← non-bypassable
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
     │  SQLITE DATABASE│  ← single file, in git repo
     │  trades, theses,│
     │  accounts, state│
     └────────┬────────┘
              │
              ▼
     ┌─────────────────┐
     │  META-CONTROLLER│  ← evaluates, culls, spawns
     │  HEAD OF DESK   │  ← synthesis, chat interface
     └─────────────────┘
              │
              ▼
     ┌─────────────────┐
     │   WEB DASHBOARD │  ← localhost:8000
     └─────────────────┘
```

### Data Layer: API-on-Demand

There is no local market data store to maintain. When an agent wakes up, it calls the Hyperliquid API directly for the data it needs at that moment:

- OHLCV (1m, 5m, 15m, 1h, 4h candles for requested assets and lookback)
- Current funding rates + 24h history
- Open interest + 24h change
- Recent liquidation data
- Current order book (top levels)

The only things stored persistently are the outputs of human decisions and agent decisions: trade records, thesis versions, performance metrics, agent state. These live in SQLite.

### Persistence: SQLite Only

A single SQLite file (`data/forge.db`) holds all persistent state. No database server. No Docker required for data. The file is small enough to commit to the repository (trade fingerprints compressed, OHLCV snapshots stored as compact binary arrays). A fresh `git clone` on a new laptop gives you the complete institutional memory of the desk.

**SQLite tables:**
- `agents` — agent registry (name, status, spawn date, cull date, config)
- `theses` — all thesis versions, all agents, including terminated ones
- `trades` — full fingerprint for every trade ever made
- `accounts` — per-agent account balance history (paper and live)
- `positions` — currently open positions (all agents, for competing-position detection)
- `reflections` — log of every thesis reflection: evidence, research, proposed changes, adversarial critique, outcome
- `evaluations` — meta-controller evaluation results per agent per cycle
- `settings` — desk-wide and per-agent settings (editable from web UI)
- `chat_history` — head-of-desk conversation history

### Risk Gate

Stateless Python validator. Every trade decision passes through it before execution. Non-bypassable by agent logic.

**Hard rules:**
- Stop loss: mandatory; must be ≥0.3% from entry price
- Liquidation price must be ≥2× stop loss distance from entry (stop out before liquidation)
- Maximum leverage: 10× hard cap (configurable lower per agent or desk-wide)
- Maximum position size: 20% of account per trade (configurable lower)
- Maximum concurrent open positions per agent: 3
- Drawdown kill: if agent account drops >15% from peak, all positions closed, agent suspended
- Competing position: if any other active agent holds a position in the same asset, entry is blocked

No exceptions. Not for high-confidence trades. Not for "exceptional" market conditions.

### Trading Bridge

```python
class TradingBridge(ABC):
    def enter(self, order: Order) -> Fill: ...
    def get_positions(self) -> list[Position]: ...
    def close(self, position_id: str, reason: str) -> Fill: ...
    def get_account(self) -> AccountState: ...

class PaperBridge(TradingBridge):
    # Simulates fill using real Hyperliquid bid/ask at time of decision
    # Updates paper account in SQLite

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
  Sharpe ratio          (target: >1.5)
  Trade frequency       (target: 3–15 per day)

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

**2. Its aggregate performance**

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
```

**3. Last 10 closed trades (with outcomes and postmortems)**

Asset, direction, entry, exit, P&L%, duration, thesis version, the hypothesis text at entry, outcome, and the agent's own one-sentence postmortem.

**4. Current open positions (own)**

Entry price, current P&L, distance to SL/TP, time open.

**5. Desk positions (all other active agents)**

```
DESK POSITIONS (other traders):
  iron_moth:    LONG ETH  @ $3,540  (+0.8%)  — entry 2h ago
  silver_basin: LONG SOL  @ $145.20 (+1.2%)  — entry 4h ago
  copper_vane:  [no positions]
  gray_finch:   SHORT BTC @ $65,100 (-0.3%)  — entry 45m ago
```

Agents cannot enter a position in any asset already held by another agent (any direction).

**6. Current market state**

- OHLCV: last 40 candles of primary timeframe for all 15 assets
- Funding rates: current + last 24h per asset
- Open interest: 24h change per asset
- Liquidation volume: last 4h per asset
- BTC dominance
- 20-period correlation matrix across the universe
- Current market regime tag

**7. Decision prompt**

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

---

## Trade Fingerprint Schema

The atomic unit of institutional memory. Written at entry, completed at close.

```json
{
  "trade_id": "jade_hawk_20250629_143712_SOL",
  "agent_id": "jade_hawk",
  "thesis_version": "v9",
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
  (Sharpe > 2.0, 100+ trades, human review)
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

---

## Seeding the First Cohort

| Agent | Seed Hypothesis |
|---|---|
| `iron_moth` | Funding rate mean reversion: persistent one-directional funding creates mechanical squeeze pressure |
| `jade_hawk` | Liquidation cascade fade: post-cascade price action reverts as market absorbs the move |
| `silver_basin` | Cross-asset lag: SOL and ARB lag BTC on breakouts by 10–20 minutes; trade the follower |
| `copper_vane` | OI divergence: price rising + OI falling = weak move, fade it |
| `gray_finch` | Session momentum: US equities open (14:30 UTC) drives crypto correlation burst; trade first candle breakout |
| `amber_wolf` | Volatility compression: ATR contracts for N candles then expands — trade the expansion direction |
| `steel_crane` | Dominance rotation: BTC dominance dropping while BTC price stable = altcoin capital rotation signal |
| `onyx_heron` | Open: generate thesis from scratch after reviewing the full trade bank |

---

## Web Dashboard

Single web application at `localhost:8000`. Started automatically with `python forge.py`. Built with FastAPI (backend) + Jinja2 templates + vanilla JS + WebSocket (real-time P&L). No Node.js. No build pipeline. No external services.

### Pages

**/ — Desk Overview**
- Portfolio aggregate: total equity, MTD return, portfolio max DD, weighted Sharpe
- Active agent leaderboard: sortable by any metric (win rate, Sharpe, PF, return, drawdown)
- Live positions panel: all open positions across all agents, current P&L (updates via WebSocket)
- System health bar: exchange connectivity, LLM status, last wakeup times per agent

**/ agents/{name} — Agent Detail**
- Status badge (ROOKIE / ACTIVE / SUSPENDED / SHADOW / LIVE)
- Equity curve (SVG, updates live)
- Full performance stats (all-time + last 20 + last 7 days + by regime)
- Open positions with live P&L
- Trade history table: filterable, sortable, click to expand full fingerprint
- Thesis tab: current thesis + version history + diff view between versions
- Reflection log: each reflection's evidence, research findings, proposed changes, adversarial critique, outcome
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
Local inference:  Ollama + Qwen3.6-35B (agent decisions, reflection, adversarial pass)
API inference:    Claude claude-sonnet-4-6 (Head of Desk synthesis, complex analysis)
Web research:     Brave Search API or SerpAPI (reflection cycles only)
Persistence:      SQLite (single file, no server, committed to git)
Web backend:      FastAPI + Jinja2 + WebSocket
Web frontend:     Vanilla HTML/CSS/JS (no build step, no npm)
Charts:           uPlot (lightweight, no dependencies, served as static file)
Scheduling:       APScheduler (in-process, no external scheduler)
Config:           YAML (desk-wide) + SQLite settings table (runtime-editable)
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
│   ├── forge.db                  ← SQLite (committed to git)
│   └── schema.sql                ← table definitions + migrations
│
├── agents/
│   ├── runtime.py                ← agent async loop (wake → decide → execute)
│   ├── decision_loop.py          ← fetch market data → build prompt → call LLM → parse
│   ├── prompt_builder.py         ← assembles full decision prompt from all context
│   ├── reflection.py             ← thesis update loop with all safeguards
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
│   ├── regime.py                 ← market regime classifier
│   └── web_research.py           ← search API client (reflection only)
│
├── risk/
│   └── gate.py                   ← stateless validator, non-bypassable
│
├── execution/
│   ├── bridge.py                 ← TradingBridge ABC
│   ├── paper_bridge.py           ← simulate fills vs real HL prices
│   └── live_bridge.py            ← real Hyperliquid order submission
│
├── store/
│   ├── db.py                     ← SQLite connection + CRUD helpers
│   ├── fingerprint.py            ← write/query/update trade fingerprints
│   ├── performance.py            ← rolling metric calculation from SQLite
│   ├── positions.py              ← desk position registry (competing position detection)
│   └── query.py                  ← structured query builder for trade bank
│
├── meta/
│   ├── controller.py             ← evaluation loop (every 6h): assess, cull, spawn
│   ├── evaluator.py              ← per-agent metric assessment + culling decisions
│   ├── spawner.py                ← new agent creation from seeds or harvested fingerprints
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
│   │   ├── chat.html
│   │   └── settings.html
│   └── static/
│       ├── forge.css
│       ├── forge.js
│       └── uplot.min.js
│
└── scripts/
    ├── fresh_start.py            ← initialize clean DB, seed all 8 agents
    ├── spawn_agent.py            ← CLI: manually create a new agent
    └── promote_agent.py          ← CLI: move agent to shadow or live mode
```

Everything lives in one GitHub repo. No external services required beyond the exchange API and Ollama (local). `git clone` → `pip install -r requirements.txt` → configure `.env` → `python forge.py` → system is running.

---

## Development Plan

Each milestone is independently demonstrable. You can start Forge after any milestone and observe meaningful behavior. Tasks within each milestone are ordered by dependency and written to be executed autonomously by an agentic developer (Firstmate) without further guidance.

---

### Milestone 1 — Walking Skeleton

**Goal:** The complete system structure exists. One agent wakes on a schedule, makes a trade decision using stub data and a stub LLM, records the fingerprint to SQLite, updates a paper account, and the web UI shows it happening. Nothing is real yet — but every seam in the architecture is proven.

**You can verify:** `python forge.py` → open `localhost:8000` → see one agent making stub trades every 60 seconds with records appearing in the UI.

**Tasks:**
1. Initialize git repository with full directory structure per the repo layout above; add `.gitignore` (exclude `.env`, `*.pyc`, `__pycache__`)
2. Write `config.yaml` with desk defaults: universe (15 assets), max leverage (10), max position size (0.20), wake interval (60s), starting balance (50000), target agent count (8)
3. Write `data/schema.sql` defining all SQLite tables: `agents`, `theses`, `trades`, `accounts`, `positions`, `reflections`, `evaluations`, `settings`, `chat_history`
4. Implement `store/db.py`: SQLite connection (WAL mode), schema initialization on first run, parameterized CRUD helpers for each table
5. Implement `market/stub.py`: returns hardcoded realistic OHLCV arrays, funding rates (-0.01 to +0.03), OI values, and liquidation volumes for all 15 assets — deterministic but plausible
6. Implement `risk/gate.py`: validates order dict (stop loss present, leverage ≤ cap, size ≤ cap, liquidation price check); raises `RiskViolation` with reason string on failure
7. Implement `execution/paper_bridge.py`: accepts validated order, simulates fill at stub mid price, writes trade to `trades` table with status='open', updates `positions` table, updates `accounts` table balance
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

### Milestone 2 — Real Market Data

**Goal:** Agents pull live market data from the Hyperliquid API. All 15 assets are priced in real time. The paper bridge simulates fills against real Hyperliquid bid/ask. The web UI shows live prices updating.

**You can verify:** Open the UI, see real BTC/ETH/SOL prices. Trigger a manual agent wake. See the trade recorded at a real Hyperliquid price. Data lag is <5 seconds.

**Tasks:**
1. Study Hyperliquid REST API docs and identify endpoints for: OHLCV candles, funding rates, open interest, recent liquidations, current order book — document base URLs and request format in `market/hyperliquid.py` header comments
2. Implement `market/hyperliquid.py`: `get_ohlcv(asset, interval, lookback_candles)`, `get_funding_rate(asset)`, `get_open_interest(asset)`, `get_liquidations(asset, hours=4)`, `get_orderbook(asset, depth=5)` — all using `httpx` async client
3. Implement rate limit handling in `market/hyperliquid.py`: exponential backoff on 429, circuit breaker after 5 consecutive failures (marks exchange as unavailable)
4. Implement `market/provider.py`: unified interface with `stub` and `hyperliquid` backends, selected by `config.yaml` flag `data_source`
5. Update `agents/prompt_builder.py` to pull real market state from provider (OHLCV + funding + OI + liquidations for all 15 assets)
6. Update `execution/paper_bridge.py` to fetch real Hyperliquid bid/ask at fill time and use mid-price for paper fill simulation
7. Implement `market/regime.py`: derives market regime tag from BTC 30-day OHLCV (trend direction + ATR percentile) → returns one of five regime strings; add regime field to all new fingerprints
8. Add `/api/prices` WebSocket endpoint to `web/app.py`: broadcasts current prices for all 15 assets every 3 seconds using Hyperliquid order book
9. Update `web/templates/base.html` to include a live price ticker bar at the top (updates via WebSocket)
10. Add `/health` endpoint returning JSON: exchange connectivity status, last successful data fetch per asset, SQLite file size, uptime
11. Update web UI overview page to show "LIVE DATA ✓" or "STUB DATA ⚠" badge based on health check
12. Stress test: verify 15-asset data pull completes within agent wake budget (< 10 seconds total)

**Done when:** Agent wakes, fetches real SOL price, places paper trade at that price, recorded in SQLite. Web UI shows live prices updating without manual refresh.

---

### Milestone 3 — Real LLM Decisions

**Goal:** Replace the stub LLM with a real local Qwen3.6-35B. One agent runs autonomously for 24+ hours making genuine trading decisions based on real market data. Every decision is logged with the agent's full reasoning text.

**You can verify:** Read the agent's reasoning in the trade log. See it skip trades when the market doesn't fit its thesis. See it reference specific funding rate values and price levels from real data.

**Tasks:**
1. Add Ollama setup instructions to README: install Ollama, `ollama pull qwen3:35b`, verify with `ollama run qwen3:35b`
2. Implement `llm/ollama_client.py`: async POST to `localhost:11434/api/chat`, streams response, extracts JSON payload from response, handles timeout (30s hard limit → return None → agent logs "LLM timeout, skipping cycle")
3. Implement `llm/client.py`: unified interface dispatching to `stub` or `ollama` based on config flag
4. Implement `agents/prompt_builder.py` performance section: calculate and format all metrics from SQLite (win rate, PF, avg win/loss, Sharpe, by-regime breakdown) using `store/performance.py`
5. Implement `store/performance.py`: all metric calculations from raw trades table (win rate, profit factor, avg win, avg loss, Sharpe of equity curve, max drawdown, by-regime breakdown)
6. Add last 10 closed trades section to decision prompt (query `trades` table, format each with asset, direction, PnL%, duration, hypothesis excerpt, outcome, postmortem)
7. Add current open positions section to decision prompt (query `positions` table for this agent)
8. Implement structured JSON response parser: validates all required fields exist in LLM output; if malformed, re-prompts with error message (max 2 retries before treating as "do nothing")
9. Implement "do nothing" path: LLM can return `{"action": "wait", "reason": "..."}` — logged to SQLite as a decision record (not a trade), included in next wake's context as recent activity
10. Implement "close early" path: LLM can return `{"action": "close", "position_id": "...", "reason": "..."}` — passes through risk gate minimum check, closes via paper bridge
11. Implement postmortem call: when a position closes (SL/TP hit or early close), make a second LLM call asking agent to write one-sentence postmortem; store in trade record
12. Run `jade_hawk` for 24 hours on real Hyperliquid data with real Qwen decisions; review: does the reasoning reference specific market conditions? Does it sometimes wait? Are stop losses always present?

**Done when:** 24h run produces 30+ trades with coherent reasoning text, some "wait" decisions, and all risk gate rules observed.

---

### Milestone 4 — Trade Fingerprint Store

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

### Milestone 5 — Multi-Agent Desk

**Goal:** All 8 initial agents run simultaneously. Competing position detection is active. The leaderboard shows all agents. No two agents hold the same asset at the same time.

**You can verify:** Watch the leaderboard update live. Trigger two agents to want the same asset — confirm one is blocked. Click into any agent to see their individual detail page.

**Tasks:**
1. Implement `store/positions.py`: `get_all_open_positions()` returns all open positions across all agents; `is_asset_held(asset)` checks if any agent currently holds a position in that asset
2. Add competing position check to `risk/gate.py`: before approving any entry, call `is_asset_held(asset)` — raise `RiskViolation("competing_position: {asset} held by {agent_id}")` if true
3. Add "DESK POSITIONS" section to `agents/prompt_builder.py`: shows all other agents' current positions (formatted as in the design doc above)
4. Implement `meta/spawner.py`: `spawn_agent(name, seed_thesis_text, config_overrides)` — creates agent record in SQLite, writes thesis file, registers in scheduler
5. Run `scripts/fresh_start.py`: initialize all 8 agents with their seed theses; document in README
6. Update `forge.py` to launch all 8 agents as concurrent `asyncio` tasks; each agent's wake schedule is offset by 30s to avoid simultaneous Hyperliquid API bursts
7. Implement per-agent configurable wake interval: read from agent's config in SQLite (or fall back to desk default)
8. Add `/` overview leaderboard: table of all agents sorted by Sharpe (default), columns: name, status, trades, win%, PF, Sharpe, weekly return, max DD, BTC corr; sortable by clicking column header
9. Implement `/agents/{name}` page: equity curve (SVG via uPlot), full stats panel, trade history table, current thesis, open positions — all reading from SQLite
10. Add agent status badge (ROOKIE / ACTIVE / SUSPENDED / SHADOW / LIVE) with color coding
11. Add `/api/desk` endpoint: returns JSON summary of all agents' current state (for WebSocket broadcasts)
12. Broadcast desk state update via WebSocket every 30 seconds; update leaderboard table in-place without page reload
13. Test: manually trigger two agents to evaluate the same asset simultaneously; confirm one is blocked by the competing position gate and the reason is logged

**Done when:** 8 agents running simultaneously. Leaderboard updates live. Competing position detection confirmed working. All agent detail pages accessible.

---

### Milestone 6 — Thesis Evolution

**Goal:** Agents self-reflect, do web research, update their theses with anti-overfitting safeguards. Full thesis version history is browsable in the UI. Manual reflection trigger works.

**You can verify:** Trigger a reflection on an agent. Watch it query its trade history, do web research, propose a thesis update, run the adversarial pass. See the new thesis version in the UI with diff highlighted.

**Tasks:**
1. Implement reflection trigger logic in `agents/runtime.py`: fire reflection when (a) N trades completed since last reflection (configurable, default 30), (b) N days elapsed since last reflection (default 14), or (c) manual trigger via API
2. Implement `market/web_research.py`: `search(query: str) -> list[str]` — calls Brave Search API (or SerpAPI), returns top 5 result summaries; requires `SEARCH_API_KEY` in `.env`
3. Implement `agents/reflection.py` main flow: (a) load evidence window + holdout window from SQLite, (b) fetch regime distribution, (c) call web research for 2-3 thesis-relevant queries, (d) build reflection prompt, (e) call LLM for updated thesis, (f) run adversarial pass, (g) run holdout validation, (h) if valid: write new thesis version; if invalid: log failure and set next reflection threshold +10 trades
4. Implement cross-agent validation in reflection: query trade bank for similar conditions from other agents; include win rate evidence in reflection prompt
5. Implement minimum trade threshold gate: hard block if < 20 trades since last reflection (return immediately with logged reason)
6. Implement thesis update throttle: hard block if < 14 days AND < 30 trades since last update
7. Implement thesis versioning: each update writes a new `agents/theses/{name}_v{N}.md` file AND a record in the `theses` SQLite table with version number, date, change summary, adversarial critique
8. Implement `POST /api/agents/{name}/reflect` endpoint: triggers reflection immediately regardless of schedule
9. Add "Reflect Now" button to agent detail page that calls this endpoint; show a progress indicator while reflection runs (WebSocket event when complete)
10. Add Thesis tab to agent detail page: current thesis text, dropdown to view any historical version, diff view between selected versions (highlight additions in green, removals in red)
11. Add Reflection Log tab to agent detail page: table of all past reflections with date, trades at time, evidence window summary, research queries and findings, proposed changes, adversarial critique, validation result (accepted/rejected), reason
12. Run a full reflection cycle on `jade_hawk` after 30 trades; verify: web research is called, adversarial pass produces meaningful critique, thesis version is saved, holdout validation runs

**Done when:** End-to-end reflection cycle completes on a live agent. New thesis version visible in UI with diff. Holdout validation pass/fail logged.

---

### Milestone 7 — Meta-Controller and Head of Desk

**Goal:** Automatic evaluation, culling, and spawning. The Head of Desk can answer questions about the desk via chat. Graveyard tracks terminated agents permanently.

**You can verify:** Let underperforming agents run until cull thresholds are hit. Watch the meta-controller suspend one automatically. Ask the head of desk "what's working?" and get a meaningful synthesis. Visit the graveyard.

**Tasks:**
1. Implement `meta/evaluator.py`: given an agent ID, compute all evaluation metrics from SQLite and compare against thresholds; return decision enum (PASS / SUSPEND / TERMINATE) with reason string
2. Implement `meta/controller.py`: APScheduler job running every 6h; iterates all ACTIVE agents (with >= 30 trades); calls evaluator; applies decision; logs to `evaluations` table
3. Implement agent suspension: sets status to SUSPENDED in SQLite, removes from scheduler, posts notification to WebSocket
4. Implement agent termination: sets status to TERMINATED, records final metrics + termination reason, marks all open positions closed at last known price, extracts 5 best-performing fingerprints as `harvest_seeds` JSON in agent record
5. Implement `meta/spawner.py` full version: accepts seed (harvest fingerprints or human-written), checks against graveyard for thesis similarity (LLM similarity check), generates new agent name, writes initial thesis using seed context, registers in scheduler
6. Implement weekly diversity spawn job (APScheduler): every 7 days, spawn 1 naive agent (blank thesis) + 1 near-clone of highest-Sharpe active agent (thesis copy with "explore variations" instruction)
7. Implement `meta/head_of_desk.py`: LLM agent (Claude claude-sonnet-4-6) with system prompt giving it full access to all desk data; tools: `query_trades(filters)`, `get_agent_summary(name)`, `get_all_agent_summaries()`, `search_graveyard()`
8. Implement `POST /api/chat` endpoint: receives user message, appends to `chat_history`, calls head-of-desk LLM with full context, streams response back via Server-Sent Events
9. Implement `/chat` page: clean chat interface, message input, streaming response display, persistent history from SQLite (loads last 20 messages on page open)
10. Add `/graveyard` page: grid of terminated agents (name, spawn date, terminate date, reason, final win rate, Sharpe, total trades, best trade); click into any for full read-only detail view
11. Add `/meta-log` page: chronological log of all meta-controller evaluation decisions, with agent name, metrics at time of decision, decision made, reason
12. Test: manually set an agent's drawdown past cull threshold in SQLite → trigger meta-controller evaluation → confirm suspension → confirm graveyard entry

**Done when:** Auto-cull fires on a real agent. Graveyard populated. Head of desk chat returns substantive answers referencing real desk data.

---

### Milestone 8 — Settings and Operational Robustness

**Goal:** All settings are configurable from the web UI and take effect immediately. The system handles exchange API failures and LLM timeouts gracefully. It recovers cleanly from a restart with no lost state.

**You can verify:** Change max leverage from 10 to 5 via Settings, hit Save, watch next agent decisions respect the new limit. Kill and restart `forge.py`, confirm agents resume from existing state with no duplicate trades.

**Tasks:**
1. Implement `settings` table fully: all desk-wide params with current value, data type, min/max, label for UI; pre-populate with defaults on schema init
2. Implement `GET /api/settings` and `PATCH /api/settings` endpoints; PATCH validates types and ranges before writing
3. Build `/settings` page with form fields for all configurable params; "Save" button calls PATCH endpoint; success/error toast notification; fields update without page reload
4. Implement live settings propagation: all agent runtimes read settings from SQLite at the start of each wake cycle (not cached in memory) so changes take effect on next wake
5. Implement graceful restart: on `forge.py` startup, read all ACTIVE/ROOKIE/SHADOW agents from SQLite and register them in the scheduler; read all OPEN positions from `positions` table and load into in-memory position registry (no positions are lost across restarts)
6. Implement exchange error handling in `market/hyperliquid.py`: 429 → wait `Retry-After` seconds; 5xx → exponential backoff (1s, 2s, 4s, max 60s); 3 consecutive failures → mark asset as stale, agent skips asset in this cycle
7. Implement LLM timeout handling: 30s hard timeout on Ollama call; on timeout, log "LLM_TIMEOUT" event to SQLite, agent does nothing this cycle, continues next scheduled wake
8. Implement position monitor: separate APScheduler task running every 5 minutes; for each open position, fetches current price and checks if SL or TP has been breached; if so, closes position via bridge (handles slippage past price target)
9. Implement emergency stop: `POST /api/emergency_stop` — closes all open positions at market (paper: mark closed at current price; live: submit market close orders); logs EMERGENCY_STOP event
10. Add emergency stop button to settings page with red styling and double-confirmation dialog
11. Implement structured logging to `logs/forge_{date}.log` with daily rotation; log format: timestamp, level, agent_id (if applicable), event_type, message; keep 14 days of logs
12. Test restart scenario: run system for 1h with open positions, kill process, restart, verify positions reload correctly and no duplicate trades created

**Done when:** Settings change takes effect on next agent wake. System restarts cleanly. Exchange downtime (simulated by blocking API) doesn't crash the process.

---

### Milestone 9 — Live Trading

**Goal:** Top-performing agents can be promoted to shadow mode (paper + live simultaneously) and then to full live trading on Hyperliquid mainnet. Full audit trail. Emergency controls work.

**You can verify:** Promote a well-performing paper agent to shadow mode. Watch it place both paper and real orders simultaneously. Compare fills. See real P&L in the UI.

**Tasks:**
1. Implement `execution/live_bridge.py`: connects to Hyperliquid mainnet using wallet private key from `.env`; implements same `TradingBridge` interface as paper bridge; submits real limit orders with limit price = current ask (for longs) to minimize slippage; polls for fill confirmation
2. Implement shadow mode: agent carries both a `PaperBridge` and `LiveBridge`; every decision is executed on both; fill details from both are recorded in `trades` table under `paper_fill` and `live_fill` columns respectively
3. Implement shadow comparison metrics in `store/performance.py`: slippage (live vs paper price), fill time (latency), partial fill rate — computed per agent in shadow mode
4. Add shadow comparison panel to agent detail page: side-by-side paper vs live fills for each trade, slippage distribution chart
5. Implement promotion workflow state machine: ACTIVE → SHADOW (requires human click + confirmation) → LIVE (requires human click + confirmation after minimum 10 shadow days)
6. Add "Promote to Shadow" button on agent detail page (only visible when agent is ACTIVE and Sharpe > 1.5 and trade count > 100); requires confirmation dialog with current metrics shown
7. Add "Go Live" button on agent detail page (only visible when agent is SHADOW and shadow period >= 10 days); shows shadow slippage stats in confirmation dialog
8. Implement separate live account balance tracking: `accounts` table distinguishes `paper` and `live` balance rows per agent; live balance initialized from real Hyperliquid account value at promotion time
9. Implement live position sizing: live positions use same `position_size_pct` as paper but applied to real live account balance
10. Implement live audit log: separate append-only `live_trades` table; entries are never updated or deleted; immutable record of all real money trades
11. Implement live emergency stop: `POST /api/emergency_stop?mode=live` submits market-close orders for all open live positions via Hyperliquid API
12. Implement trade alert: on live position open/close or live DD > 5%, send notification via webhook (configurable URL in `.env`; payload is JSON trade summary) — webhook enables Discord/Slack/email via Zapier or similar
13. Test: promote a paper agent to shadow for 24h, review fill comparison report, confirm live orders are visible in Hyperliquid UI

**Done when:** First agent in shadow mode with real orders visible on Hyperliquid. Paper vs live comparison report shows reasonable slippage numbers.

---

### Milestone 10 — Production Hardening

**Goal:** Forge runs unattended for weeks. Setup from scratch takes under 30 minutes on a new machine. Docker available for clean deployment.

**You can verify:** Follow the README setup steps on a clean machine (or VM) from git clone to running system in < 30 minutes.

**Tasks:**
1. Write comprehensive `README.md`: prerequisites (Python 3.11+, Ollama, Hyperliquid account), step-by-step install, `.env` configuration guide, first run walkthrough, how to add/remove agents, how to promote to live, FAQ
2. Write `docs/ops.md`: operations playbook — how to: add a new agent manually, cull an agent manually, recover from SQLite corruption, check system health, interpret logs, handle exchange downtime, interpret culling decisions
3. Implement startup self-check in `forge.py`: before starting any agents, verify (a) Hyperliquid API reachable, (b) Ollama running and Qwen model loaded, (c) SQLite readable/writable, (d) all required `.env` vars present — print clear error message and exit if any check fails
4. Create `Dockerfile`: Python 3.11 base, install requirements, set entrypoint to `python forge.py`
5. Create `docker-compose.yml`: `forge` service (Dockerfile), `ollama` service (official Ollama image with Qwen model volume); services start in correct order; port 8000 exposed for web UI
6. Add `data/backups/` directory (git-ignored): implement daily SQLite backup job in APScheduler (`cp data/forge.db data/backups/forge_{date}.db`), retain 30 days
7. Implement SQLite database migrations: `data/migrations/` directory with numbered `.sql` files; on startup, check current schema version and apply any pending migrations; prevents manual DB surgery on updates
8. Load test: simulate 8 agents all waking within the same second (set all wake intervals to 1s for 60s); verify no SQLite lock contention, no duplicate fingerprints, response times acceptable
9. Security audit: confirm no secrets appear in any log file; confirm `.env` and `data/forge.db` are in `.gitignore`; confirm `.env.example` has all required keys with placeholder values and comments
10. Create `scripts/fresh_start.py`: wipes existing `forge.db` (with confirmation prompt), re-runs schema init, seeds all 8 initial agents with their thesis files, prints "Ready. Run: python forge.py"
11. Add system metrics to `/health` endpoint: CPU usage, memory usage, SQLite file size, agent wake success rate (last hour), LLM response time (p50/p95 last hour), exchange API latency (p50/p95 last hour)
12. Add metrics panel to Settings page pulling from `/health`: green/yellow/red status indicators for each subsystem with last-check timestamp

**Done when:** Fresh git clone on a new machine, following README, produces a running system with all 8 agents active within 30 minutes. Docker Compose brings up the full stack with one command.

---

## Known Risks

| Risk | Mitigation |
|---|---|
| Agents overfit to recent noise | 7-layer anti-overfitting system in thesis loop |
| All agents converge on same thesis | Competing position blocking forces distinct focus; diversity spawns maintain breadth; Head of Desk flags thesis convergence |
| Regime shift breaks all agents simultaneously | Regime tagging surfaces regime-specific strategies; meta-controller suspends agents failing in new regime |
| Hyperliquid API downtime | Circuit breaker; agents skip cycle on unavailability; positions monitored by separate process |
| Qwen inference too slow for 15m cadence | 30s hard timeout; async execution; "do nothing" fallback preserves account safety |
| Live order submission failure | Paper bridge always succeeds; live bridge failure logs and alerts but does not corrupt paper state |
| SQLite file grows too large for git | OHLCV compressed with msgpack; archiving job compresses old fingerprints; 10,000 trades ≈ 100MB |
| Hyperliquid regulatory status changes for US | All agent logic is exchange-agnostic; bridge can swap to Kraken Futures or similar with one config change |

---

*Forge is not a prediction machine. It is an evolutionary system that puts AI traders under genuine competitive pressure, gives them the tools to learn from the communal record of every trade ever made, and gets out of the way. The market decides who has edge. The desk decides who gets capital.*
