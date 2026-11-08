import json
from pathlib import Path

import httpx
import pytest

from war_room_engine.db import Database
from war_room_engine.scanners import PacketEmitter, PolymarketScanner


class DummyTracker:
    def hits_for_asset(self, asset_id, within_seconds=300):
        return []


@pytest.fixture()
def emitter(tmp_path):
    db = Database(tmp_path / "t.db")
    return PacketEmitter(db=db, evidence_path=tmp_path / "PIPELINE-EVIDENCE.jsonl")


def test_emitter_dedup_and_rate(emitter, tmp_path):
    base = {
        "desk": "polymarket",
        "asset_id": "asset-1",
        "ticker_or_slug": "x",
        "x_velocity_score": 10,
        "stage2_metrics_score": 10,
    }
    assert emitter.emit(dict(base)) is True
    # Same asset within cooldown → reject
    assert emitter.emit(dict(base, packet_id="pkt_other")) is False
    # Different assets up to rate limit
    assert emitter.emit(dict(base, asset_id="asset-2")) is True
    assert emitter.emit(dict(base, asset_id="asset-3")) is False  # rate window full
    lines = (tmp_path / "PIPELINE-EVIDENCE.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert emitter.db.get_candidate(json.loads(lines[0])["packet_id"]) is not None


@pytest.mark.asyncio
async def test_polymarket_scanner_safe_compounder(emitter, monkeypatch):
    market = {
        "conditionId": "0xcond1",
        "slug": "will-x-happen",
        "question": "Will X happen?",
        "liquidityNum": 25000,
        "volume24hr": 12000,
        "clobTokenIds": json.dumps(["tokYES", "tokNO"]),
        "outcomePrices": json.dumps(["0.15", "0.85"]),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "gamma-api" in str(request.url):
            return httpx.Response(200, json=[market])
        if "clob.polymarket.com/book" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "bids": [{"price": "0.14", "size": "500"}],
                    "asks": [{"price": "0.16", "size": "400"}],
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        scanner = PolymarketScanner(emitter, DummyTracker(), http=http)
        n = await scanner.scan_once()
    assert n == 1
    pending = emitter.db.list_pending_candidates()
    assert len(pending) == 1
    pkt = json.loads(pending[0]["packet_json"])
    assert pkt["trigger"] == "safe_compounder"
    assert pkt["desk"] == "polymarket"
    assert pkt["side"] == "NO"
    # NO must be priced in NO-token space (complement of YES 0.14/0.16 → 0.84/0.86).
    assert pkt["entry_price"] == pkt["ask"]
    assert abs(pkt["ask"] - 0.86) < 1e-9
    assert pkt["take_profit_price"] > pkt["entry_price"] > pkt["stop_loss_price"]
    assert "kelly_pct" in pkt
