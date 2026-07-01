"""Tests for forge.py's heartbeat-cycle wrapper used both for the immediate
startup run and the recurring APScheduler job."""
import httpx
import pytest
import respx

import forge
from market.provider import MarketProvider


UNIVERSE = [
    "BTC-PERP", "ETH-PERP", "SOL-PERP", "SUI-PERP", "AVAX-PERP", "LINK-PERP",
    "AAVE-PERP", "BNB-PERP", "ARB-PERP", "OP-PERP", "TAO-PERP", "FET-PERP",
    "RENDER-PERP", "XRP-PERP", "XLM-PERP", "TIA-PERP", "HYPE-PERP", "LTC-PERP",
    "BCH-PERP", "ADA-PERP",
]


@pytest.mark.asyncio
@respx.mock
async def test_run_heartbeat_cycle_writes_file_and_logs(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    respx.get("https://api.alternative.me/fng/?limit=1").mock(
        return_value=httpx.Response(200, json={"data": [{"value": "50"}]})
    )
    config = {
        "data_source": "stub",
        "universe": UNIVERSE,
        "desk": {"heartbeat_path": "data/heartbeat.json"},
    }
    provider = MarketProvider(config)
    async with provider:
        with caplog.at_level("INFO", logger="forge"):
            await forge.run_heartbeat_cycle(provider, config)

    from market.heartbeat import read_heartbeat
    packet = read_heartbeat("data/heartbeat.json")
    assert packet is not None
    assert len(packet["assets"]) == len(UNIVERSE)
    assert any("Heartbeat cycle complete" in r.message for r in caplog.records)
