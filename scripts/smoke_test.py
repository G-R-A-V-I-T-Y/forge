"""scripts/smoke_test.py — convenience runner for the R10 smoke harness.

Runs tests/test_smoke.py: seeding → heartbeat → decisions (LLM, compiled,
benchmarks) → risk gate → paper fill → wick reconcile close with shared-cost
fees/funding → wait-candidate capture → counterfactual replay → state
snapshot.  This is the pre-run gate's final verification step: run it before
every unattended start.

Usage:  python scripts/smoke_test.py
"""
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

if __name__ == "__main__":
    sys.exit(
        subprocess.call(
            [sys.executable, "-m", "pytest", "tests/test_smoke.py", "-v"],
            cwd=str(PROJECT_ROOT),
        )
    )
