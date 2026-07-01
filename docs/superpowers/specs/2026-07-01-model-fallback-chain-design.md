# Ordered Model Fallback Chain for Trading Decisions

Status: implemented. Branch `feat/model-fallback-chain`.

## Problem

`config.yaml`'s `llm_backend` selects a single fixed backend (`stub` or
`ollama`) via `llm/client.py`. The captain wants every agent's decision call
to instead try an ordered list of models — free/cheap remote tiers first,
falling back through progressively more available options, with local
Ollama as the last-resort local model — recording which model actually
answered, and failing loudly (not a silent "wait") if literally nothing in
the chain responds.

## Verified facts (checked directly in this environment before implementing)

- `opencode models` lists all six required model IDs exactly as named in
  the brief: `openrouter/anthropic/claude-sonnet-5`,
  `opencode/deepseek-v4-flash-free`, `opencode/big-pickle`,
  `opencode/mimo-v2.5-free`, `opencode/north-mini-code-free`,
  `opencode/nemotron-3-ultra-free`.
- `opencode auth list` shows one configured credential: OpenRouter — so the
  `openrouter/anthropic/claude-sonnet-5` tier is reachable.
- `opencode run --help` confirms `--variant` (reasoning effort, e.g. `low`)
  and `--format json`.
- `curl http://localhost:11434/api/tags` confirms Ollama is up locally with
  `qwen3.6:35b_optimized` loaded, matching `llm/ollama_client.py`'s
  `MODEL` constant.

### `opencode run --format json` output shape (observed directly, not assumed)

`opencode run --model <id> [--variant <v>] --format json "<message>"` prints
newline-delimited JSON events to stdout, e.g.:

```
{"type":"step_start", ...}
{"type":"text", "part":{"type":"text","text":"{\"action\": \"wait\", ...}"}}
{"type":"step_finish", ...}
```

or, on failure (e.g. unknown model id):

```
{"type":"error","error":{"name":"UnknownError","data":{"message":"..."}}}
```
(exit code 1)

`llm/model_chain.py` parses each stdout line as JSON, concatenates the
`part.text` of every `"type":"text"` event (in order) to reconstruct the
model's full text response, and treats any `"type":"error"` event or
non-zero exit code as tier failure (log a warning, fall through).

There is no separate system/user framing flag on `opencode run` — the
positional `message` is a single string. Per the brief's suggestion,
`system_prompt` and `decision_prompt` are concatenated into one message
(`system_prompt + "\n\n" + decision_prompt`), matching how Firstmate itself
is launched.

## Chain order and mechanism (`llm/model_chain.py`)

```python
CHAIN = [
    Tier("opencode", "openrouter/anthropic/claude-sonnet-5", "low", "Claude Sonnet 5 (low)"),
    Tier("opencode", "opencode/deepseek-v4-flash-free", None, "DeepSeek V4 Flash Free"),
    Tier("opencode", "opencode/big-pickle", None, "Big Pickle"),
    Tier("opencode", "opencode/mimo-v2.5-free", None, "MiMo V2.5 Free"),
    Tier("opencode", "opencode/north-mini-code-free", None, "North Mini Code Free"),
    Tier("opencode", "opencode/nemotron-3-ultra-free", None, "Nemotron 3 Ultra Free"),
    Tier("ollama", None, None, "Ollama qwen3.6:35b_optimized"),
]
```

`Tier` is a small `NamedTuple` (`kind, model_id, variant, display_name`) —
readable, not deeply nested config, no magic strings repeated elsewhere.

For each `"opencode"` tier: run `opencode run --model <model_id>
[--variant <variant>] --format json "<message>"` as a subprocess with a
**60-second timeout**. Free-tier/rate-limited remote models can be slow or
occasionally down; 60s is generous enough to absorb normal latency (the
6 real remote tiers in live verification below all responded in under 10s)
without hanging a decision cycle for multiple minutes on a dead tier. On
timeout, non-zero exit, unparseable output, or a JSON decision missing
required fields, log a warning and fall through — never raise.

JSON extraction reuses `llm/ollama_client.py`'s `_extract_json()` (imported,
not copy-pasted) to pull the trading-decision object out of the model's
text response — same code path Ollama already uses.

For the `"ollama"` tier: calls `llm/client.py`'s existing `_ollama_decide()`
helper unchanged. Because `_ollama_decide()` already collapses every Ollama
failure mode (timeout, connection error, unparseable JSON) into the
sentinel `{"action": "wait", "reason": "LLM unavailable or timed out"}`
rather than raising or returning `None`, `model_chain.decide()` detects that
exact sentinel to know the Ollama tier itself failed (rather than the model
legitimately deciding to wait) and falls through to the final "no model
available" result. This is a deliberate coupling to `_ollama_decide()`'s
literal reason string, documented here and at the call site — the
alternative (bypassing `_ollama_decide()` to call `ollama_client.decide()`
directly and check for `None`) would have required re-async-bridging logic
that already exists in `llm/client.py`, which the brief asked not to
reimplement.

`decide(system_prompt, decision_prompt, config) -> tuple[dict, str | None]`
is synchronous (matches `llm/client.py`'s `_ollama_decide()` sync-wrapping
pattern, and `agents/decision_loop.py`'s existing non-awaited `llm_fn(...)`
call convention). It tries tiers 1-6, then tier 7, returning
`(decision_dict, display_name)` for the first success. If all 7 fail:
`({"action": "error", "reason": "no model available"}, None)`.

## `decide()` signature/return-shape change and propagation

`llm_fn`'s contract changes system-wide from `(system_prompt,
decision_prompt) -> dict` to `(system_prompt, decision_prompt) -> tuple[dict,
str | None]`.

- `forge.py`'s `llm_fn` now calls `model_chain.decide(...)` instead of
  `llm_client.decide(...)`, returning the tuple directly.
- `agents/decision_loop.py`'s `_call_llm_with_retry()` unpacks the tuple on
  each attempt, tracks the most recent non-`None` `model_used` seen across
  retries (so even a final retry-exhaustion failure still reports which
  model actually responded last), and short-circuits immediately (no
  retry-reprompting) when the decision's `action == "error"` — that's the
  explicit "no model available" signal from `model_chain.decide()`, not a
  malformed-JSON case that reprompting could fix.
- `run_decision()` receives `(response, model_used)` from
  `_call_llm_with_retry()`. It computes a `model_label` for persistence:
  the real `model_used` if the chain reported one, else the literal string
  `"no model available"` if the decision's action is `"error"`, else `None`
  (only in the pre-existing "retries exhausted on malformed JSON" case,
  which the model chain's own validation makes rare). Whenever
  `model_label` is not `None`, `run_decision()` calls
  `store.db.update_last_model_used(conn, agent_id, model_label)` —
  unconditionally, before dispatching on the decision's action, so
  wait/close/enter/error cycles all update it, per the captain's
  "most recently used model" framing (not "model used for the last trade").
  A new explicit `if action == "error": return {"action": "error", "detail": ...}`
  branch surfaces the failure to the runtime/UI instead of the previous
  generic-degrade-to-wait behavior.
- On `"enter"`, the resolved `model_used` is passed through to
  `store/fingerprint.py`'s `write_entry(..., model_used=model_used)` — a new
  keyword parameter with a `None` default, following the same style as the
  earlier `market_context` addition.
- `run_postmortem()`'s `llm_fn(...)` call also unpacks the tuple (ignoring
  the model label — postmortems aren't tracked per-model, out of scope per
  the brief) so it doesn't break under the new contract.
- `llm/client.py`'s `decide()` (the old single-backend stub/ollama
  dispatcher) is untouched and still returns a plain `dict` — it is not
  `forge.py`'s primary wiring anymore, but nothing else in the codebase
  called `forge.py`'s `llm_fn` directly except tests, which were updated
  for the new tuple shape. `llm/client.py`'s own test suite
  (`tests/test_llm_client.py`) keeps asserting the old plain-dict shape,
  since `llm_client.decide()` itself didn't change.

## Schema additions

- `agents.last_model_used TEXT` — updated after every decision cycle.
- `trades.model_used TEXT` — set at trade entry time via `write_entry()`.

Both are added to `data/schema.sql`'s `CREATE TABLE` statements (for fresh
databases) and to idempotent `ALTER TABLE ... ADD COLUMN` migrations in
`store/db.py` (`_AGENTS_MIGRATION_COLUMNS`, extending
`_TRADES_MIGRATION_COLUMNS`), following the exact pattern already
established for the Milestone 4 OHLCV-column migration — `PRAGMA table_info`
to detect existing columns, `ALTER TABLE ADD COLUMN` for anything missing,
safe to call on every `init_schema()` invocation including against a
pre-existing local `data/forge.db`.

`store/query.py`'s `query_trades()` already does `SELECT * FROM trades`, so
`model_used` flows through automatically once the column exists — no code
change needed there beyond the schema/migration.

## Web UI

- `overview.html`'s `#leaderboard` gets a new "Model" column showing
  `last_model_used`, falling back to an em-dash when null (no decision
  cycle yet) and a red/warning badge when the literal
  `"no model available"` sentinel is present.
- `web/app.py`'s `overview()`, `api_desk()`, and the `/api/ws/desk`
  WebSocket broadcast all include `last_model_used` per agent, alongside
  the existing per-agent field set.
- `trade_bank.html`'s per-trade fingerprint expand view shows `model_used`
  next to the existing hypothesis/expected-value/confidence fields, with
  the same null/error-sentinel rendering rules.

## Adaptation found during live testing: opencode's default agent is unusable for this purpose

Initial live-verification calls (`opencode run --model <id> --format json
"<system+decision message>"`, no `--agent` flag) were unreliable in a way
not anticipated by the brief: opencode's default **"build"** agent has a
strong baked-in system prompt identifying itself as *"OpenCode, a coding
assistant CLI tool"* with full read/bash/edit tool access into the current
working directory (the forge repo itself, since that's `forge.py`'s cwd).
Given our system+decision message as a single user turn:

- Weaker free models frequently responded with generic
  `{"status": "ready", ...}` or `{"message": "Awaiting instructions..."}`
  acknowledgments, apparently expecting a follow-up dev task rather than
  answering in-character as a trader.
- Claude Sonnet 5, being more safety-aligned, explicitly **refused**:
  *"I'm OpenCode, a coding assistant CLI tool — not a trading-decision
  API, and I don't have live market data or the ability to make trading
  decisions. I won't roleplay as one..."*
- Non-deterministically, a model would instead attempt a `read` tool call
  on the project directory; since `_run_opencode_tier()`'s subprocess has
  no TTY, opencode auto-rejects the permission request and the turn ends
  with no text output — `model_chain.py` correctly treats this as tier
  failure and falls through, but it's a real, observed failure mode.

**Fix**: a project-scoped custom opencode agent, committed at
`.opencode/agent/trading-responder.md` (opencode auto-discovers
`.opencode/agent/*.md` from the working directory — verified directly,
no external/global config needed). It denies every tool permission
(`write`/`edit`/`bash`/`read`/`grep`/`glob`/`list`/`webfetch`/`todowrite`
all `false`) and replaces the "OpenCode coding assistant" system prompt
with a headless JSON-decision-responder identity that explicitly won't
explore files, ask questions, or refuse to answer in-character. Every
`_run_opencode_tier()` call passes `--agent trading-responder`. This
eliminated the tool-call-permission-denial failure mode and Claude's
identity-based refusal in manual single-call testing.

A second real Windows-specific bug found and fixed during verification:
`subprocess.run(..., text=True)` without an explicit `encoding=` defaults
to the console's locale codepage (`cp1252` in this environment), which
raises `UnicodeDecodeError` in the stdout-reader thread on non-ASCII bytes
opencode writes — observed live as a silent ~60s stall (the reader thread
died, so `subprocess.run` blocked until the timeout) instead of a prompt
response. Fixed by passing `encoding="utf-8", errors="replace"`.

## Live verification summary — the honest result

All 6 remote `opencode` tiers were called for real (not mocked, no
network mocking) via `scripts/verify_model_chain_live.py`, one call each,
with the `trading-responder` agent wired in. This was run three times
with progressively simpler/more explicit prompts (full results and every
raw response are in the PR description) to rule out a prompt-wording
artifact before concluding.

**Result: all 6 tiers are reachable — no auth failures, no "model not
found" errors, no rate-limit rejections — but under this synthetic,
minimal single-shot test prompt, none of the 6 consistently returned a
schema-compliant `{"action": "wait"/"enter"/"close", ...}` decision.**
Even with an explicit literal example schema in the prompt and the
custom no-tools agent, most models hedged with a "no market data
provided" or "awaiting input" response instead of using the given
price/funding/OI numbers, despite those numbers being present in the
prompt. Two tiers occasionally timed out (60s) rather than answering at
all. Manual single-shot ad hoc tests with an even simpler one-schema
prompt did succeed intermittently for some tiers (e.g. Claude Sonnet 5
and Big Pickle both returned exactly the requested JSON in isolated
manual runs) — so this is not a hard "these models can never produce
valid JSON" finding, but a real reliability/consistency gap: schema
adherence via headless single-shot `opencode run` calls is inconsistent
across tiers with a minimal test prompt, in a way full production
decision prompts (rich with 40 OHLCV candles, correlation matrices,
desk-wide position context, an explicit "Output JSON only" instruction
already present in `agents/persona.py`/`agents/prompt_builder.py`) may or
may not fully avoid — this needs to be monitored once live in production,
not assumed fixed by this PR.

Every one of these failures was correctly detected and rejected by
`_is_valid_decision()` / `_run_opencode_tier()`'s parsing, and the chain's
fallthrough logic worked exactly as designed in every single case
(confirmed both by the mocked unit tests and by watching the real run
correctly move tier-to-tier). The system's resilience mechanism is sound;
the open question the captain should know about is real-world schema
compliance of the specific free-tier models routed through opencode,
which this PR surfaces honestly rather than paper over.

Full per-tier latency table from the final (simplest-prompt) run is in
the PR description.
