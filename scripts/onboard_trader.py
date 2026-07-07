"""scripts/onboard_trader.py — Generate Hyperliquid wallets for agents.

Generates Ethereum wallets for agents that don't have one yet, encrypts them
as keystore files, and registers the addresses in the database.

Usage:
    python scripts/onboard_trader.py                            # onboard all paper agents
    python scripts/onboard_trader.py --agent jade_hawk          # onboard one agent
    python scripts/onboard_trader.py --list                     # show onboard status
    python scripts/onboard_trader.py --password-file <path>     # read password from file
    python scripts/onboard_trader.py --set-live jade_hawk       # set live_enabled=1
    python scripts/onboard_trader.py --set-paper jade_hawk      # set live_enabled=0

The master keystore password is resolved (first wins):
  1. --password-file <path>
  2. FORGE_KEYSTORE_PASSWORD environment variable
  3. Interactive prompt
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import eth_account
from eth_account import Account

from store.db import get_connection

KEYSTORE_DIR = Path("data/keystores")
DB_PATH = Path("data/forge.db")


def _resolve_password(password_file: str | None) -> str:
    if password_file:
        return Path(password_file).read_text(encoding="utf-8").strip()
    env_pw = os.environ.get("FORGE_KEYSTORE_PASSWORD")
    if env_pw:
        return env_pw
    pw = getpass.getpass("Keystore master password: ")
    confirm = getpass.getpass("Confirm password: ")
    if pw != confirm:
        print("Passwords do not match.", file=sys.stderr)
        sys.exit(1)
    return pw


def list_agents(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, status, wallet_address, keystore_path, live_enabled "
        "FROM agents ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]


def onboard_agent(conn, agent_id: str, password: str) -> dict:
    existing = conn.execute(
        "SELECT wallet_address, keystore_path FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()
    if existing and existing["wallet_address"]:
        return {
            "agent_id": agent_id,
            "address": existing["wallet_address"],
            "keystore_path": existing["keystore_path"] or "",
            "skipped": True,
        }

    account = Account.create()
    KEYSTORE_DIR.mkdir(parents=True, exist_ok=True)
    keystore = Account.encrypt(account.key.hex(), password)
    keystore_path = KEYSTORE_DIR / f"{agent_id}.json"
    keystore_path.write_text(json.dumps(keystore), encoding="utf-8")
    conn.execute(
        "UPDATE agents SET wallet_address = ?, keystore_path = ? WHERE id = ?",
        (account.address, str(keystore_path), agent_id),
    )
    conn.commit()
    return {
        "agent_id": agent_id,
        "address": account.address,
        "keystore_path": str(keystore_path),
        "skipped": False,
    }


def _set_live(conn, agent_id: str, enabled: bool) -> bool:
    row = conn.execute(
        "SELECT wallet_address FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()
    if not row:
        print(f"Agent '{agent_id}' not found.", file=sys.stderr)
        return False
    if not row["wallet_address"]:
        print(
            f"Agent '{agent_id}' has no wallet yet. Run onboard_trader.py first.",
            file=sys.stderr,
        )
        return False
    conn.execute(
        "UPDATE agents SET live_enabled = ? WHERE id = ?",
        (1 if enabled else 0, agent_id),
    )
    conn.commit()
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Onboard agents to live Hyperliquid trading"
    )
    parser.add_argument("--agent", help="Onboard a specific agent by id")
    parser.add_argument("--list", action="store_true", help="Show onboard status")
    parser.add_argument("--password-file", help="Read keystore password from file")
    parser.add_argument("--set-live", metavar="AGENT", help="Set agent to live trading")
    parser.add_argument("--set-paper", metavar="AGENT", help="Set agent back to paper")
    args = parser.parse_args()

    conn = get_connection(str(DB_PATH))
    try:
        if args.list:
            agents = list_agents(conn)
            if not agents:
                print("No agents found in database.")
                return
            hdr = f"{'Agent':<20} {'Status':<10} {'Wallet':<44} {'Live':<6} {'Keystore'}"
            print(hdr)
            print("-" * len(hdr))
            for a in agents:
                wallet = a["wallet_address"] or "(none)"
                live = "YES" if a["live_enabled"] else "no"
                ks = a["keystore_path"] or ""
                print(
                    f"{a['id']:<20} {a['status']:<10} {wallet:<44} {live:<6} {ks}"
                )
            return

        if args.set_live:
            ok = _set_live(conn, args.set_live, True)
            if ok:
                print(f"+ {args.set_live} set to live trading")
            return

        if args.set_paper:
            ok = _set_live(conn, args.set_paper, False)
            if ok:
                print(f"+ {args.set_paper} set to paper trading")
            return

        password = _resolve_password(args.password_file)

        if args.agent:
            agent_ids = [args.agent]
        else:
            rows = conn.execute(
                "SELECT id FROM agents WHERE wallet_address IS NULL ORDER BY name"
            ).fetchall()
            agent_ids = [r["id"] for r in rows]

        if not agent_ids:
            print("All agents already have wallets.")
            return

        results = []
        for aid in agent_ids:
            result = onboard_agent(conn, aid, password)
            results.append(result)
            flag = " (skipped, already exists)" if result["skipped"] else ""
            print(f"  + {result['agent_id']:<20} {result['address']}{flag}")

        new_results = [r for r in results if not r["skipped"]]
        if new_results:
            print("\n=== AGENTS READY - FUNDING REQUIRED ===")
            print(
                "Send USDC (Arbitrum One) to each address below, then bridge to the"
            )
            print("Hyperliquid perp account at https://app.hyperliquid.xyz/bridge\n")
            for r in new_results:
                print(f"  {r['agent_id']:<20} {r['address']}")
            print("\nAfter funding, enable live trading with:")
            for r in new_results:
                print(f"  python scripts/onboard_trader.py --set-live {r['agent_id']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
