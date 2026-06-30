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

## Testing

- `pytest.ini` / `pyproject.toml` not present; asyncio mode defaults to STRICT in the installed plugin version.
- Tests that use `@pytest.mark.asyncio` need `asyncio_mode = "auto"` or the decorator; current tests use the decorator.
- `respx` mock library is used for HTTP mocking in `tests/test_hyperliquid.py`.
