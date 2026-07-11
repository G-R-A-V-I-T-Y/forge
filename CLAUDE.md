# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Python environment

- On the development machine, `python` in PATH resolves to the Windows stub (`C:\Windows\System32\python`) which silently no-ops. Always use the full path: `C:\ProgramData\Anaconda3\python.exe`.
- Run tests with: `C:\ProgramData\Anaconda3\python.exe -m pytest tests/ -v`
- `respx` and `pytest-asyncio` are installed in the Anaconda Python (user site: `C:\Users\chris\Python\Python311\site-packages`).

## Architecture

- `market/stub.py` — deterministic in-memory market data. Contains module-level `get_market_state()` (used by existing AgentRuntime tests) and the `StubMarket` async class implementing the provider interface.
- `market/hyperliquid.py` — async `HyperliquidClient` with circuit breaker (5 failures → open, 60s cooldown) and rate limit retry (3 attempts, honours `Retry-After`). Concurrency capped at 10 via asyncio.Semaphore.
- `market/provider.py` — `MarketProvider` facade; selects backend via `config["data_source"]` (`"stub"` or `"hyperliquid"`).
- `config.yaml` `data_source: stub` keeps the default backend as stub so all existing tests pass unmodified.
- `market/heartbeat.py` `append_historical()` mirrors every heartbeat packet as a JSON line into `data/historical_data/{YYYY-MM-DD}.jsonl` (dir is a hardcoded constant, not config; gitignored). It swallows all exceptions by design — the historical capture must never block the primary `write_heartbeat()` path.
- `scripts/build_training_dataset.py` is an offline, read-only batch job (not wired into `forge.py` or the live heartbeat cycle) that flattens `data/historical_data/*.jsonl` into `data/historical_data/training_dataset.parquet`: one row per (asset, timestamp) with forward-looking labels (return, realized vol, max drawdown/run-up, funding accrued, illustrative SL/TP stop-hit) at configurable horizons. A (sample, horizon) combination is excluded (labels left null, row kept) if the heartbeat timeline has a gap or doesn't yet extend far enough beyond a 2x-cadence staleness threshold. Requires `pyarrow` (added to `requirements.txt`).

## Testing

- `pytest.ini` / `pyproject.toml` not present; asyncio mode defaults to STRICT in the installed plugin version.
- Tests that use `@pytest.mark.asyncio` need `asyncio_mode = "auto"` or the decorator; current tests use the decorator.
- `respx` mock library is used for HTTP mocking in `tests/test_hyperliquid.py`.
- Do NOT use `asyncio.get_event_loop()` in tests — Python 3.11+ raises `RuntimeError: There is no current event loop` in non-main threads. Use `@pytest.mark.asyncio` + `async def` or `asyncio.run()` instead.

## Local LLM server (llm/llama_server.py)

- Empirically established: qwen3.6:35b_optimized with thinking ON takes 162–290 s per decision. Thinking OFF (`--reasoning off` at llama-server startup) drops to ~12–20 s with 148–282 completion tokens and no quality loss.
- Batch-size 2048 / ubatch-size 1024 gives ~2.9× prefill speedup over defaults. Thread count (6 vs 24) had no measured decode-speed benefit.
- Real prompts run ~10,800 tokens; default context-size in settings is 24,576 (safe headroom). `MIN_CONTEXT_SIZE = 12288` is the validation floor.
- `llm/llama_server.py` exports a module-level singleton `server_manager`; `forge.py` starts/stops it and `web/app.py` exposes start/stop/status API endpoints.
- `llm/llama_server_client.py` calls `http://localhost:{port}/v1/chat/completions` (OpenAI-compat). Timeout is 60 s (thinking off; previously 900 s for Ollama with thinking on).
- Settings are persisted in the `settings` SQLite table (already in schema.sql). `store/settings.py` wraps it with typed get/set and merge-over-defaults.
- `llm/model_chain.py` now dynamically loads the chain from settings via `get_chain()` at each `decide()` call so Settings → Save & Apply takes effect in the next agent cycle. Default final tier is `llama_server`, not `ollama`.
- `test_forge_agent_timeout.py` and `test_forge_heartbeat_schedule.py` fail in this Python env because `apscheduler` is not installed in Anaconda3 but IS installed in the forge venv. Always ignore them when running from Anaconda3: `--ignore=tests/test_forge_agent_timeout.py --ignore=tests/test_forge_heartbeat_schedule.py`.

## R10 — Smoke-test harness (real as of 2026-07-11)

- `tests/test_smoke.py` — integration smoke test over the real composition: `fresh_start.seed_desk` + benchmark seeding on a file-backed temp DB, real `generate_heartbeat` over `StubMarket`, `run_decision` for a pure-LLM agent / a compiled agent / both benchmarks, risk gate → PaperBridge fill, wick-based SL reconcile closing the trade with shared-cost-model fees and funding, wait-candidate capture → deterministic counterfactual replay, state snapshot. Only pure-external I/O (Fear & Greed HTTP) and repo-dirtying paths (specs/theses/OI-history dirs) are redirected.
- `scripts/smoke_test.py` — convenience runner: `python scripts/smoke_test.py` (or `python -m pytest tests/test_smoke.py -v`).
- The smoke test is the pre-run gate's final verification step: run it before every unattended start. Note: an earlier revision of this section documented the harness before it existed — the 2026-07-11 pre-run review (assessment §9, F5) caught that; treat "documented" as unverified until you have run the command yourself.

## R11 — Repo hygiene (completed 2026-07-10)

- Root debris removed: `query_trades.py`, `query_trades2.py`, `quick_test.py`, `test_import.py`, `test_event_import.py`, `test_syntax.bat`, `test_unwrapping.py` deleted (nothing useful remained).
- `.omo/` and `.claude/worktrees/` added to `.gitignore`; all 13 stale worktrees pruned.
- Orphan junk theses removed from `agents/theses/`: `agent_mean_reversion_2_v1.md`, `agent_momentum_1_v1.md`, `amber_wolf_v1.md`, `config_test_v1.md`, `dupe_agent_v1.md`, `gray_finch_v1.md`, `test_trader_v1.md`.

## Config convention (standing rule)

- All desk-level config keys live under `desk.` in `config.yaml` (e.g. `desk.max_leverage`, `desk.starting_balance`).
- Module-level defaults **must fail loudly** on missing keys — never silently invent numbers. Callers read via `config.get("desk", {})` or `config["desk"]`, never `config.get("desk_config")` (that key does not exist).
- The single seed-path rule: there is exactly ONE way to seed agents (`scripts/fresh_start.py`) and ONE way to seed benchmarks (`scripts/seed_benchmarks.py`). No inline seeding in other modules.
